"""Owner-approved dispute outcome, real isolated PostgreSQL and HMAC traffic.
Only an external FX quote is fixed. No live transfer or partner HTTP is sent.
"""
import asyncio
from contextlib import asynccontextmanager
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import pytest
from sqlalchemy import select,func
from sqlalchemy.ext.asyncio import AsyncSession
from app import models as m
from app.services import appeals,rolling
from app.services.deposit_confirmation import confirm_deposit_payment
from app.services.deposit_lifecycle import finalize_unsuccessful_deposit
from app.services.ledger import manual_trader_adjustment
from tests.test_integration_modes_postgres import isolated as base_isolated,client,token_login
from tests.test_final_acceptance_combined_postgres import fixture,snapshot,reconcile,assert_paid
from tests.test_acceptance_financial_postgres import request
from tests.test_aggregator_credentials_postgres import issue,payment
from tests.test_rolling_postgres import _strict_quote

@asynccontextmanager
async def isolated(monkeypatch):
    from redis.asyncio import Redis
    from app.core.config import settings
    from app.core import auth_hardening
    async with base_isolated(monkeypatch) as data:
        redis=Redis.from_url(settings.REDIS_URL,decode_responses=True)
        monkeypatch.setattr(auth_hardening,'_redis',redis)
        assert await redis.ping()
        try:yield data
        finally:await redis.aclose()

@pytest.fixture(autouse=True)
def quote(monkeypatch):
    async def fixed():return _strict_quote(datetime.now(timezone.utc),'100')
    monkeypatch.setattr(rolling,'get_strict_rolling_ask_quote',fixed)

async def operation(engine,actor,c,origin):
    ids=await fixture(engine,actor)
    if origin=='merchant':
        r=await request(ids['key'],ids['secret'],uuid.uuid4().hex)
        assert r.status_code==200,r.text
        oid=uuid.UUID(r.json()['id'])
    else:
        async with AsyncSession(engine,expire_on_commit=False) as db:
            account=m.AggregatorAccount(platform_merchant_id=ids['merchant'],name='Synthetic appeal '+uuid.uuid4().hex,
                api_key='unissued_'+uuid.uuid4().hex,secret_hash='',callback_url='https://8.8.8.8/callback')
            db.add(account);await db.flush()
            db.add(m.FeeRule(entity_type='aggregator',entity_id=account.id,fee_side='executor_fee',method='sbp',payment_method='sbp',currency='RUB',
                min_amount=D('0'),percent=D('5'),rate_percent=D('5'),effective_from=datetime.now(timezone.utc)-timedelta(days=1)))
            await db.commit();ids['aggregator']=account.id
        key,secret=await issue(engine,account,actor,'sandbox')
        r=await payment(c,key,secret);assert r.status_code==200,r.text
        oid=uuid.UUID(r.json()['platform_payment_id'])
    return ids,oid

async def finalize(engine,ids,oid,prior):
    async with AsyncSession(engine,expire_on_commit=False) as db:
        if prior=='paid':
            result=await confirm_deposit_payment(db,oid,actor_id=ids['trader'],actor_ip='127.0.0.1',audit_action='synthetic_initial_paid',description='Synthetic payment')
            assert result.confirmed
        elif prior!='pending':
            dep=await db.scalar(select(m.Deposit).where(m.Deposit.id==oid).with_for_update())
            await finalize_unsuccessful_deposit(db,dep,reason='synthetic_'+prior,target_status=prior)
            await db.commit()

async def open_appeal(c,oid):
    r=await c.post('/api/v1/cabinet/appeals',json={'operation_type':'deposit','operation_id':str(oid),'message':'Synthetic payment evidence'})
    assert r.status_code==200,r.text
    return uuid.UUID(r.json()['id'])

async def resolve(c,aid):
    return await c.post(f'/api/v1/admin/appeals/{aid}/resolve',json={'status':'approved','decision':'Owner-approved synthetic test'})

async def partner_state(engine,ids,oid):
    async with AsyncSession(engine) as db:
        payment=await db.scalar(select(m.AggregatorPayment).where(m.AggregatorPayment.platform_payment_id==oid))
        if not payment:return None
        account=await db.get(m.AggregatorAccount,ids['aggregator'])
        callbacks=list(await db.scalars(select(m.AggregatorCallbackLog).where(m.AggregatorCallbackLog.related_payment_id==payment.id)))
        return {'payment':payment.status,'paid_at':payment.paid_at,'balance':account.balance,'turnover':account.total_turnover,
            'callbacks':sorted((str(x.id),x.direction,x.payload_json) for x in callbacks)}

@pytest.mark.parametrize('origin',['merchant','aggregator'])
@pytest.mark.parametrize('prior',['paid','pending','cancelled','expired','failed'])
def test_rule_both_origins_rolling_referrals_tariff_change_retry_and_concurrency(monkeypatch,origin,prior):
    async def run():
        async with isolated(monkeypatch) as (engine,_,users,_keys),client() as c:
            actor=users['superadmin'];await token_login(c,actor)
            ids,oid=await operation(engine,actor,c,origin);await finalize(engine,ids,oid,prior)
            before=await snapshot(engine,ids,oid);partner_before=await partner_state(engine,ids,oid)
            aid=await open_appeal(c,oid)
            # Later tariff edits must not alter the saved operation economics.
            async with AsyncSession(engine) as db:
                trader=await db.get(m.User,ids['trader']);trader.trader_commission_percent=D('99')
                rules=list(await db.scalars(select(m.FeeRule).where(m.FeeRule.entity_id.in_([ids['trader'],ids['merchant'],ids.get('aggregator',ids['trader'])]))))
                for rule in rules:rule.rate_percent=D('40');rule.percent=D('40')
                await db.commit()
            responses=await asyncio.wait_for(asyncio.gather(resolve(c,aid),resolve(c,aid)),20)
            assert sorted(r.status_code for r in responses)==[200,409],[r.text for r in responses]
            after=await snapshot(engine,ids,oid);partner_after=await partner_state(engine,ids,oid)
            if prior=='paid':
                assert after==before and partner_after==partner_before
            else:
                assert after['operation']=='paid' and after['hold_status']=='settled'
                if origin=='merchant':assert_paid(after)
                else:
                    assert after['trader']==(D('99000'),D('0')) # Aggregator fee is not trader income.
                    assert after['merchant']==(D('1300'),D('0'))
                    assert after['rolling']==(D('6'),D('6'),D('0'),'exhausted')
                    assert after['allocation']==('paid',D('6'),D('600'),D('300'))
                    assert after['fees']==(D('100'),D('50'),D('50'),'settled')
                    assert after['teamlead']==(D('5'),D('0'),D('0'),D('5'))
                    assert after['accruals']==[('merchant','credited',D('5'))]
                    platform=[(r[1],r[2]) for r in after['journals']['platform_ledger_entries']]
                    assert sum(v for k,v in platform if k=='platform_income')-sum(v for k,v in platform if k=='teamlead_expense')==D('45')
                    assert partner_after['payment']=='paid' and partner_after['balance']==50 and partner_after['turnover']==1000
                    new_callbacks=[x for x in partner_after['callbacks'] if x[0] not in {y[0] for y in partner_before['callbacks']}]
                    assert len(new_callbacks)==2
                    assert {x[1] for x in new_callbacks}=={'platform_to_aggregator','aggregator_to_merchant'}
                    assert all(x[2]['status']=='paid' for x in new_callbacks)
                new_events=[x for x in after['events'] if x[0] not in {y[0] for y in before['events']}]
                assert len(new_events)==1 and new_events[0][1:]==('deposit.paid','paid')
                assert len(after['journals']['merchant_rolling_transfer_consumptions'])==2
                assert sum(x[1]=='deposit_success_debit' for x in after['journals']['trader_ledger_entries'])==1
            async with AsyncSession(engine) as db:
                appeal=await db.get(m.Appeal,aid);assert appeal.status=='approved'
                assert appeal.metadata_json['finance_applied'] is (prior!='paid')
                assert appeal.metadata_json['financial_resolution']==('already_paid' if prior=='paid' else 'new_payment')
                assert await db.scalar(select(func.count()).select_from(m.AuditLog).where(m.AuditLog.target_id==str(aid),m.AuditLog.action=='deposit_appeal_approved'))==1
            assert (await resolve(c,aid)).status_code==409
            assert await snapshot(engine,ids,oid)==after and await partner_state(engine,ids,oid)==partner_after
            await reconcile(engine,ids)
    asyncio.run(run())

@pytest.mark.parametrize('origin',['merchant','aggregator'])
@pytest.mark.parametrize('prior',['pending','cancelled','expired'])
def test_insufficient_funds_or_active_reserve_is_atomic(monkeypatch,origin,prior):
    async def run():
        async with isolated(monkeypatch) as (engine,_,users,_keys),client() as c:
            actor=users['superadmin'];await token_login(c,actor);ids,oid=await operation(engine,actor,c,origin)
            await finalize(engine,ids,oid,prior);aid=await open_appeal(c,oid)
            async with AsyncSession(engine) as db:
                trader=await db.get(m.User,ids['trader'])
                if prior=='pending':
                    # Explicit isolated corruption: this order cannot consume another order's hold.
                    trader.trader_hold=D('1')
                else:
                    # 900 available is insufficient even though total balance is 1000.
                    await manual_trader_adjustment(db,trader,target_balance=D('1000'),target_hold=D('100'),idempotency_prefix=uuid.uuid4().hex,reason='Synthetic other obligation')
                await db.commit()
            before=await snapshot(engine,ids,oid);partner=await partner_state(engine,ids,oid)
            for _ in range(2):assert (await resolve(c,aid)).status_code==409
            assert await snapshot(engine,ids,oid)==before and await partner_state(engine,ids,oid)==partner
            async with AsyncSession(engine) as db:assert (await db.get(m.Appeal,aid)).status=='opened'
    asyncio.run(run())

@pytest.mark.parametrize('previous',[None,'','appeal_opened','unrecognized'])
def test_unknown_previous_state_never_treated_as_unpaid(monkeypatch,previous):
    async def run():
        async with isolated(monkeypatch) as (engine,_,users,_keys),client() as c:
            actor=users['superadmin'];await token_login(c,actor);ids,oid=await operation(engine,actor,c,'merchant');aid=await open_appeal(c,oid)
            async with AsyncSession(engine) as db:
                appeal=await db.get(m.Appeal,aid);appeal.metadata_json={'previous_deposit_status':previous};await db.commit()
            before=await snapshot(engine,ids,oid);r=await resolve(c,aid)
            assert r.status_code==409 and 'previous operation state' in r.text
            assert await snapshot(engine,ids,oid)==before
    asyncio.run(run())

@pytest.mark.parametrize('origin',['merchant','aggregator'])
def test_paid_during_dispute_or_second_dispute_cannot_credit_again(monkeypatch,origin):
    async def run():
        async with isolated(monkeypatch) as (engine,_,users,_keys),client() as c:
            actor=users['superadmin'];await token_login(c,actor);ids,oid=await operation(engine,actor,c,origin);aid=await open_appeal(c,oid)
            # Confirmation and appeal approval contend for the same Deposit lock.
            async def normal_confirm():
                from app.services.deposit_confirmation import DepositConfirmationConflict
                try:await finalize(engine,ids,oid,'paid')
                except DepositConfirmationConflict:pass
            outcomes=await asyncio.wait_for(asyncio.gather(resolve(c,aid),normal_confirm()),20)
            assert outcomes[0].status_code==200,outcomes[0].text
            before=await snapshot(engine,ids,oid);partner=await partner_state(engine,ids,oid)
            new_aid=await open_appeal(c,oid);assert (await resolve(c,new_aid)).status_code==200
            assert await snapshot(engine,ids,oid)==before and await partner_state(engine,ids,oid)==partner
            assert sum(x[1]=='deposit_success_debit' for x in before['journals']['trader_ledger_entries'])==1
            assert sum(e[1]=='deposit.paid' for e in before['events'])==1
            await reconcile(engine,ids)
    asyncio.run(run())


def test_automatic_batch_late_failure_rolls_back_all_finance(monkeypatch):
    async def run():
        async with isolated(monkeypatch) as (engine,_,users,_keys),client() as c:
            actor=users['superadmin'];await token_login(c,actor);ids,oid=await operation(engine,actor,c,'merchant');aid=await open_appeal(c,oid)
            async with AsyncSession(engine) as db:
                appeal=await db.get(m.Appeal,aid);appeal.metadata_json={**appeal.metadata_json,'deadline_at':(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()};await db.commit()
            before=await snapshot(engine,ids,oid)
            real=appeals.settle_deposit_credit
            async def fail_after_postings(*args,**kwargs):
                await real(*args,**kwargs)
                raise ValueError('Synthetic late failure after all financial postings')
            monkeypatch.setattr(appeals,'settle_deposit_credit',fail_after_postings)
            async with AsyncSession(engine,expire_on_commit=False) as db:
                events=await appeals.approve_expired_appeals(db);await db.commit();assert events==[]
            assert await snapshot(engine,ids,oid)==before
            async with AsyncSession(engine) as db:
                appeal=await db.get(m.Appeal,aid);assert appeal.status=='opened' and 'Synthetic late failure' in appeal.metadata_json['auto_approve_error']
            monkeypatch.setattr(appeals,'settle_deposit_credit',real)
            async with AsyncSession(engine) as db:
                events=await appeals.approve_expired_appeals(db);await db.commit();assert len(events)==1
            assert_paid(await snapshot(engine,ids,oid));await reconcile(engine,ids)
    asyncio.run(run())


@pytest.mark.parametrize('missing',['snapshot','reserve'])
def test_missing_saved_financial_evidence_is_fail_closed(monkeypatch,missing):
    async def run():
        async with isolated(monkeypatch) as (engine,_,users,_keys),client() as c:
            actor=users['superadmin'];await token_login(c,actor);ids,oid=await operation(engine,actor,c,'merchant');aid=await open_appeal(c,oid)
            async with AsyncSession(engine) as db:
                if missing=='snapshot':
                    fee=await db.scalar(select(m.OperationFeeSnapshot).where(m.OperationFeeSnapshot.deposit_id==oid))
                    await db.delete(fee)
                else:
                    dep=await db.get(m.Deposit,oid);meta=dict(dep.metadata_json);meta.pop('trader_hold_amount');dep.metadata_json=meta
                await db.commit()
            async with AsyncSession(engine) as db:
                trader=await db.get(m.User,ids['trader']);funds=(trader.trader_balance,trader.trader_hold)
                journal_count=await db.scalar(select(func.count()).select_from(m.TraderLedgerEntry).where(m.TraderLedgerEntry.trader_id==ids['trader']))
            assert (await resolve(c,aid)).status_code==409
            async with AsyncSession(engine) as db:
                trader=await db.get(m.User,ids['trader']);assert (trader.trader_balance,trader.trader_hold)==funds
                assert await db.scalar(select(func.count()).select_from(m.TraderLedgerEntry).where(m.TraderLedgerEntry.trader_id==ids['trader']))==journal_count
                assert (await db.get(m.Appeal,aid)).status=='opened'
    asyncio.run(run())


def test_legacy_gross_hold_debits_saved_net_and_releases_difference(monkeypatch):
    async def run():
        from app.services.ledger import trader_hold
        async with isolated(monkeypatch) as (engine,_,users,_keys),client() as c:
            actor=users['superadmin'];await token_login(c,actor);ids,oid=await operation(engine,actor,c,'merchant')
            async with AsyncSession(engine) as db:
                dep=await db.get(m.Deposit,oid);trader=await db.get(m.User,ids['trader'])
                await trader_hold(db,trader,D('50'),oid,'synthetic-extra-hold-'+str(oid),'Synthetic historical full reserve')
                dep.metadata_json={**dep.metadata_json,'trader_hold_amount':'1000.00'};await db.commit()
            aid=await open_appeal(c,oid);r=await resolve(c,aid);assert r.status_code==200,r.text
            after=await snapshot(engine,ids,oid);assert_paid(after)
            assert any(x[2]==D('50') and x[3]=='trader-appeal-profit-release:'+str(oid) for x in after['journals']['trader_ledger_entries'])
            await reconcile(engine,ids)
    asyncio.run(run())


def test_automatic_paid_dispute_with_rolling_changes_no_money_or_events(monkeypatch):
    async def run():
        async with isolated(monkeypatch) as (engine,_,users,_keys),client() as c:
            actor=users['superadmin'];await token_login(c,actor);ids,oid=await operation(engine,actor,c,'merchant');await finalize(engine,ids,oid,'paid')
            before=await snapshot(engine,ids,oid);aid=await open_appeal(c,oid)
            async with AsyncSession(engine) as db:
                appeal=await db.get(m.Appeal,aid);appeal.metadata_json={**appeal.metadata_json,'deadline_at':(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()};await db.commit()
            for _ in range(2):
                async with AsyncSession(engine) as db:
                    assert await appeals.approve_expired_appeals(db)==[];await db.commit()
            assert await snapshot(engine,ids,oid)==before
            async with AsyncSession(engine) as db:
                appeal=await db.get(m.Appeal,aid);assert appeal.status=='approved' and appeal.decision=='approved_auto'
                assert not appeal.metadata_json['finance_applied']
            await reconcile(engine,ids)
    asyncio.run(run())
