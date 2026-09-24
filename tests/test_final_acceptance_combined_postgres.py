"""Combined Appeal + both TeamLead referrals + Rolling acceptance.

Only the guarded local test DB is used. No reset, direct money/state writes,
real transfers, or financial mocks. The external Rapira quote alone is fixed.
Funding, routing, cancellation, confirmation and resolution use normal commands.
"""
import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from urllib.parse import urlsplit

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app import models as m
from app.core.security import encrypt_secret, hash_password
from app.db.session import engine as application_engine
from app.services.deposit_confirmation import confirm_deposit_payment
from app.services.finance_reconciliation import reconcile_merchant_balance, reconcile_trader_balance
from app.services.ledger import credit, manual_trader_adjustment
from app.services.rolling import confirm_rolling_transfer, register_rolling_transfer, reconcile_rolling_account
from app.services.teamlead import (
    create_or_replace_assignment, create_or_replace_merchant_assignment,
    reconcile_teamlead_account, reverse_teamlead_accruals_for_deposit,
)
from tests.test_acceptance_financial_postgres import request, finish, replay_client_per_event_loop
from tests.test_acceptance_known_gaps import authenticated
from tests.test_rolling_postgres import _strict_quote


async def fixture(engine, actor):
    tag = uuid.uuid4().hex
    key, secret = 'combined-' + tag, 'synthetic-' + tag
    async with AsyncSession(engine, expire_on_commit=False) as db:
        priority = int(await db.scalar(select(func.coalesce(func.max(m.User.trader_traffic_priority), 0)))) + 1
        owner = m.User(email=f'combined-merchant-{tag}@example.test', role='merchant', password_hash=hash_password(tag))
        lead = m.User(email=f'combined-lead-{tag}@example.test', role='teamlead', password_hash=hash_password(tag))
        trader = m.User(email=f'combined-trader-{tag}@example.test', role='trader', password_hash=hash_password(tag),
                        trader_traffic_status='active', trader_traffic_priority=priority)
        db.add_all([owner, lead, trader]); await db.flush()
        merchant = m.Merchant(owner_id=owner.id, name='Synthetic combined acceptance', sandbox_mode=True)
        db.add(merchant); await db.flush()
        trader.trader_assigned_merchants = [str(merchant.id)]
        db.add(m.ApiKey(merchant_id=merchant.id, api_key=key, secret_hash=encrypt_secret(secret), mode='sandbox'))
        db.add(m.Requisite(trader_id=trader.id, owner_name='Synthetic acceptance', full_name='Synthetic acceptance',
                           method='sbp', bank_code='sberbank', bank_name='СберБанк', value_encrypted=encrypt_secret('+79990001122'),
                           enabled=True, status='active', min_check=D('1'), max_check=D('100000'),
                           simultaneous_limit=10, daily_limit=D('1000000'), request_count=100, operation_limit=100))
        for kind, identifier, side, rate in [('merchant', merchant.id, 'merchant_fee', '10'), ('trader', trader.id, 'executor_fee', '5')]:
            db.add(m.FeeRule(entity_type=kind, entity_id=identifier, fee_side=side, method='sbp', payment_method='sbp',
                            currency='RUB', min_amount=D('0'), percent=D(rate), rate_percent=D(rate),
                            effective_from=datetime.now(timezone.utc)-timedelta(minutes=1)))
        # Zero-valued model defaults; all opening funds have a real journal.
        await manual_trader_adjustment(db, trader, target_balance=D('100000'), target_hold=D('0'),
                                       idempotency_prefix='combined-opening-' + tag, reason='Synthetic acceptance opening')
        await credit(db, merchant.id, D('1000'), idempotency_key='combined-opening-' + tag,
                     description='Synthetic acceptance opening')
        await create_or_replace_assignment(db, teamlead_id=lead.id, trader_id=trader.id, commission_percent=D('1'),
                                           actor_id=actor.id, reason='Combined acceptance')
        await create_or_replace_merchant_assignment(db, teamlead_id=lead.id, merchant_id=merchant.id,
                                                    commission_percent=D('0.5'), actor_id=actor.id, reason='Combined acceptance')
        transfer_ids = []
        for amount in ('2', '4'):
            transfer = await register_rolling_transfer(
                db, merchant_id=merchant.id, actor_id=actor.id, amount_usdt=D(amount), network='TRC20',
                destination_address='TJRabPrwbZy45sbavfcjinPJC18kjpRTv8', tx_hash=f'synthetic-{tag}-{amount}',
                sent_at=datetime.now(timezone.utc), comment='Synthetic evidence; no transfer performed',
                idempotency_key=f'combined-funding-{tag}-{amount}')
            await confirm_rolling_transfer(db, transfer_id=transfer.id, merchant_id=merchant.id, actor_id=owner.id)
            transfer_ids.append(transfer.id)
        await db.commit()
        return dict(key=key, secret=secret, merchant=merchant.id, trader=trader.id, lead=lead.id, transfers=transfer_ids)


async def snapshot(engine, ids, oid):
    async with AsyncSession(engine) as db:
        dep = await db.get(m.Deposit, oid)
        trader = await db.get(m.User, ids['trader'])
        merchant = await db.scalar(select(m.Balance).where(m.Balance.merchant_id == ids['merchant']))
        lead = await db.scalar(select(m.TeamLeadBalance).where(m.TeamLeadBalance.teamlead_id == ids['lead']))
        account = await db.scalar(select(m.MerchantRollingAccount).where(m.MerchantRollingAccount.merchant_id == ids['merchant']))
        allocation = await db.scalar(select(m.MerchantRollingAllocation).where(m.MerchantRollingAllocation.deposit_id == oid))
        fee = await db.scalar(select(m.OperationFeeSnapshot).where(m.OperationFeeSnapshot.deposit_id == oid))
        transfers = [await db.get(m.MerchantRollingTransfer, tid) for tid in ids['transfers']]
        journals = {}
        for cls, condition, amount_field in [
            (m.LedgerEntry, m.LedgerEntry.merchant_id == ids['merchant'], 'amount'),
            (m.TraderLedgerEntry, m.TraderLedgerEntry.trader_id == ids['trader'], 'amount'),
            (m.PlatformLedgerEntry, m.PlatformLedgerEntry.operation_id == oid, 'amount'),
            (m.TeamLeadLedgerEntry, m.TeamLeadLedgerEntry.teamlead_id == ids['lead'], 'amount_rub'),
            (m.MerchantRollingLedgerEntry, m.MerchantRollingLedgerEntry.merchant_id == ids['merchant'], 'amount_usdt'),
            (m.MerchantRollingTransferConsumption, m.MerchantRollingTransferConsumption.deposit_id == oid, 'amount_usdt'),
        ]:
            rows = list(await db.scalars(select(cls).where(condition)))
            journals[cls.__tablename__] = sorted((str(r.id), r.entry_type, getattr(r, amount_field), r.idempotency_key) for r in rows)
        accruals = []
        for cls, source in [(m.TeamLeadAccrual, 'trader'), (m.TeamLeadMerchantAccrual, 'merchant')]:
            row = await db.scalar(select(cls).where(cls.deposit_id == oid))
            if row: accruals.append((source, row.status, row.accrual_rub))
        events = sorted((str(e.id), e.event_type, e.payload['status']) for e in await db.scalars(
            select(m.WebhookEvent).where(m.WebhookEvent.merchant_id == ids['merchant'])))
        return {
            'operation': dep.status, 'hold_status': dep.metadata_json['trader_hold_status'],
            'trader': (trader.trader_balance, trader.trader_hold), 'merchant': (merchant.available, merchant.frozen),
            'teamlead': (lead.available_rub, lead.frozen_rub, lead.debt_rub, lead.total_earned_rub),
            'rolling': (account.principal_usdt, account.recovered_usdt, account.outstanding_usdt, account.status),
            'allocation': (allocation.status, allocation.rolling_applied_usdt, allocation.rolling_applied_rub, allocation.settle_credited_rub),
            'transfers': [(t.remaining_usdt, t.recovered_usdt) for t in transfers],
            'fees': (fee.merchant_fee_amount, fee.executor_fee_amount, fee.platform_income_amount, fee.settlement_status),
            'accruals': accruals, 'journals': journals, 'events': events,
        }


def assert_paid(result):
    assert result['operation'] == 'paid'
    assert result['hold_status'] == 'settled'
    assert result['trader'] == (D('99050'), D('0'))
    assert result['merchant'] == (D('1300'), D('0'))
    assert result['teamlead'] == (D('15'), D('0'), D('0'), D('15'))
    assert result['rolling'] == (D('6'), D('6'), D('0'), 'exhausted')
    assert result['allocation'] == ('paid', D('6'), D('600'), D('300'))
    assert result['transfers'] == [(D('0'), D('2')), (D('0'), D('4'))]
    assert result['fees'] == (D('100'), D('50'), D('50'), 'settled')
    assert result['accruals'] == [('trader', 'credited', D('10')), ('merchant', 'credited', D('5'))]
    platform = [(r[1], r[2]) for r in result['journals']['platform_ledger_entries']]
    assert sorted(platform) == sorted([('executor_fee', D('50')), ('platform_income', D('50')),
                                       ('teamlead_expense', D('10')), ('teamlead_expense', D('5'))])
    assert sum(amount for kind, amount in platform if kind == 'platform_income') - sum(
        amount for kind, amount in platform if kind == 'teamlead_expense') == D('35')
    assert [(r[1], r[2]) for r in result['journals']['trader_ledger_entries']].count(('deposit_success_debit', D('950'))) == 1
    assert len(result['journals']['merchant_rolling_transfer_consumptions']) == 2


async def reconcile(engine, ids):
    async with AsyncSession(engine) as db:
        trader = await reconcile_trader_balance(db, ids['trader'])
        merchant = await reconcile_merchant_balance(db, ids['merchant'])
        rolling = await reconcile_rolling_account(db, ids['merchant'])
        lead = await reconcile_teamlead_account(db, ids['lead'])
        assert trader.ok, trader
        assert merchant.ok, merchant
        assert rolling.ok, rolling
        assert lead['reconciled'], lead


@pytest.mark.parametrize(('prior', 'decision'), [
    ('pending', 'approved'), ('pending', 'rejected'),
    ('cancelled', 'approved'), ('cancelled', 'rejected'),
    ('paid', 'rejected'), ('paid', 'approved'),
    ('cancelled', 'approved_then_referral_reversal'),
])
def test_combined_appeal_teamlead_rolling(monkeypatch, caplog, prior, decision):
    import app.services.rolling as rolling_service

    async def quote():
        return _strict_quote(datetime.now(timezone.utc), '100')

    monkeypatch.setattr(rolling_service, 'get_strict_rolling_ask_quote', quote)

    async def run():
        url = os.environ['TEST_DATABASE_URL']; parsed = urlsplit(url)
        assert os.environ['ENV'] == 'test'
        assert parsed.hostname == '127.0.0.1' and parsed.port == 55432 and parsed.path == '/tradespace_test'
        await application_engine.dispose(close=False)
        engine = create_async_engine(url)
        # Real Redis per asyncio.run: no stale pooled connection or auth fallback.
        from redis.asyncio import Redis
        from app.core import auth_hardening
        from app.core.config import settings
        auth_redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
        monkeypatch.setattr(auth_hardening, '_redis', auth_redis)
        assert await auth_redis.ping()
        try:
            async with authenticated(engine, 'superadmin') as (client, actor):
                ids = await fixture(engine, actor)
                external = 'combined-' + uuid.uuid4().hex
                created = await request(ids['key'], ids['secret'], external)
                assert created.status_code == 200, created.text
                oid = uuid.UUID(created.json()['id'])
                initial = await snapshot(engine, ids, oid)
                assert initial['trader'] == (D('100000'), D('950'))
                assert initial['merchant'] == (D('1000'), D('0'))
                assert initial['teamlead'] == (D('0'), D('0'), D('0'), D('0'))
                assert initial['rolling'] == (D('6'), D('0'), D('6'), 'active')
                assert initial['allocation'] == ('pending', D('0'), D('0'), D('0'))
                assert initial['fees'][:3] == (D('100'), D('50'), D('50'))
                await reconcile(engine, ids)
                if prior == 'cancelled':
                    cancelled = await request(ids['key'], ids['secret'], external, cancel=True)
                    assert cancelled.status_code == 200, cancelled.text
                elif prior == 'paid':
                    async with AsyncSession(engine, expire_on_commit=False) as db:
                        confirmed = await confirm_deposit_payment(db, oid, actor_id=ids['trader'], actor_ip='127.0.0.1',
                                                                  audit_action='combined_confirm', description='Synthetic acceptance')
                        assert confirmed.confirmed
                before = await snapshot(engine, ids, oid)
                assert before['operation'] == prior
                if prior == 'cancelled':
                    assert before['hold_status'] == 'released' and before['trader'] == (D('100000'), D('0'))
                    assert before['allocation'][0] == 'released'
                if prior == 'paid': assert_paid(before)
                opened = await client.post('/api/v1/cabinet/appeals', json={
                    'operation_type': 'deposit', 'operation_id': str(oid), 'message': 'Synthetic combined appeal evidence'})
                assert opened.status_code == 200, opened.text
                aid = uuid.UUID(opened.json()['id'])
                path = f'/api/v1/admin/appeals/{aid}/resolve'
                selected = 'approved' if decision.endswith('referral_reversal') else decision
                data = {'status': selected, 'decision': 'Synthetic combined acceptance'}
                responses = await asyncio.wait_for(asyncio.gather(client.post(path, json=data), client.post(path, json=data)), 20)
                assert sorted(r.status_code for r in responses) == [200, 409], [r.text for r in responses]
                after = await snapshot(engine, ids, oid)
                if selected == 'approved':
                    assert_paid(after)
                else:
                    assert {k: v for k, v in after.items() if k != 'events'} == {k: v for k, v in before.items() if k != 'events'}
                expected_status = 'paid' if selected == 'approved' else prior
                new_events = [e for e in after['events'] if e[0] not in {e[0] for e in before['events']}]
                if prior == 'paid' and selected == 'approved':
                    assert new_events == [] and after == before
                else:
                    assert len(new_events) == 1 and new_events[0][1:] == (f'deposit.{expected_status}', expected_status)
                async with AsyncSession(engine) as db:
                    appeal = await db.get(m.Appeal, aid)
                    assert appeal.status == selected
                    assert appeal.metadata_json['finance_applied'] is (selected == 'approved' and prior != 'paid')
                    assert await db.scalar(select(func.count()).select_from(m.AppealMessage).where(m.AppealMessage.appeal_id == aid)) == 2
                    assert await db.scalar(select(func.count()).select_from(m.AuditLog).where(
                        m.AuditLog.target_id == str(aid), m.AuditLog.action == 'appeal_resolved')) == 1
                assert (await client.post(path, json=data)).status_code == 409
                assert await snapshot(engine, ids, oid) == after
                await reconcile(engine, ids)
                if decision.endswith('referral_reversal'):
                    for attempt in range(2):
                        async with AsyncSession(engine, expire_on_commit=False) as db:
                            rows = await reverse_teamlead_accruals_for_deposit(db, deposit_id=oid, actor_id=actor.id,
                                                                            reason='Synthetic explicit referral correction')
                            assert len(rows) == 2
                            await db.commit()
                        corrected = await snapshot(engine, ids, oid)
                        assert corrected['teamlead'] == (D('0'), D('0'), D('0'), D('0'))
                        assert corrected['accruals'] == [('trader', 'reversed', D('10')), ('merchant', 'reversed', D('5'))]
                        for field in ('operation', 'hold_status', 'trader', 'merchant', 'rolling', 'allocation', 'transfers', 'fees', 'events'):
                            assert corrected[field] == after[field]
                        assert len(corrected['journals']['teamlead_ledger_entries']) == 4
                        reversals = [r[2] for r in corrected['journals']['platform_ledger_entries'] if r[1] == 'teamlead_expense_reversal']
                        assert sorted(reversals) == [D('5'), D('10')]
                        if attempt: assert corrected == first_correction
                        else: first_correction = corrected
                        await reconcile(engine, ids)
                print('COMBINED_ACCEPTANCE ' + json.dumps({
                    'prior': prior, 'decision': decision, 'result': 'PASS',
                    'before': {k: v for k, v in before.items() if k not in {'journals', 'events'}},
                    'after': {k: v for k, v in after.items() if k not in {'journals', 'events'}},
                    'new_event': new_events[0][1:] if new_events else None, 'reconciled': ['trader', 'merchant', 'rolling', 'teamlead'],
                    'duplicate_resolution': 409, 'concurrent_resolution': [200, 409],
                    'explicit_referral_correction_checked': decision.endswith('referral_reversal'),
                }, default=str, ensure_ascii=False))
                assert not any(r.getMessage() in {'auth_lock_redis_unavailable', 'rate_limit_backend_error'} for r in caplog.records)
        finally:
            await auth_redis.aclose()
            await finish(engine)
    asyncio.run(run())
