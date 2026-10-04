"""QR-1.1: real PostgreSQL transactions, isolated synthetic operations only."""
import asyncio
import base64
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import json
import uuid
import jwt
import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app import models as m
from app.core.security import reveal_text
from app.services import appeals
from app.services.deposit_confirmation import confirm_deposit_payment, DepositConfirmationConflict
from app.services.deposit_lifecycle import expire_due_deposits
from app.services.fee_tiers import COMMISSION_TIER_RANGES
from tests.test_appeal_confirmation_rule_postgres import isolated, open_appeal
from tests.test_integration_modes_postgres import client, token_login
from tests.test_qr1 import prepare, create, result, image_png, PAYLOAD
from tests.test_merchant_hmac_v2_postgres import _signed_headers
from tests.tradespace.test_wave4_teamlead import login, csrf

METHODS = ['sbp', 'c2c', 'mobile_commerce', 'sbp_qr']

async def method_operation(engine, merchant, users, keys, c, method):
    await prepare(engine, merchant, users, 'merchant')
    async with AsyncSession(engine) as db:
        req = await db.scalar(select(m.Requisite)); req.method = method
        for rule in await db.scalars(select(m.FeeRule)):
            rule.method = rule.payment_method = method
        await db.commit()
    tag = uuid.uuid4().hex
    path = '/api/v1/merchant/deposits'
    body = json.dumps(dict(external_id=tag, amount='1000', currency='RUB', method=method)).encode()
    key, secret = keys['sandbox']
    headers = _signed_headers(api_key=key, secret=secret, body=body, nonce=uuid.uuid4().hex, idempotency_key=tag, signed_path=path)
    r = await c.post(path, content=body, headers=headers)
    assert r.status_code == 200, r.text
    return uuid.UUID(r.json()['id'])

async def outcome(engine, oid, trader_id):
    async with AsyncSession(engine) as db:
        dep = await db.get(m.Deposit, oid); trader = await db.get(m.User, trader_id)
        balance = await db.scalar(select(m.Balance).where(m.Balance.merchant_id == dep.merchant_id))
        return (dep.status, dep.qr_reserved, trader.trader_balance, trader.trader_hold,
                balance.available, balance.frozen,
                tuple(sorted((str(e.id), e.event_type) for e in await db.scalars(select(m.WebhookEvent)))),
                await db.scalar(select(func.count()).select_from(m.TraderLedgerEntry)),
                await db.scalar(select(func.count()).select_from(m.LedgerEntry)))

@pytest.mark.parametrize('method', METHODS)
@pytest.mark.parametrize('role', ['trader', 'operator'])
def test_trader_decline_forbidden_every_method_http(monkeypatch, method, role):
    async def run():
        async with isolated(monkeypatch) as (engine, merchant, users, keys), client() as c:
            oid = await method_operation(engine, merchant, users, keys, c, method)
            if role == 'operator':
                async with AsyncSession(engine) as db:
                    actor = await db.get(m.User, users['trader'].id)
                    actor.role = role
                    await db.commit()
                users['trader'].role = role
            await login(c, users['trader'], realm='trader')
            page = await c.get('/trader/cabinet/tradespace/operations/' + str(oid))
            assert page.status_code == 200 and 'Не оплачено' not in page.text
            before = await outcome(engine, oid, users['trader'].id)
            for path in (f'/trader/cabinet/deposits/{oid}/decline', f'/cabinet/deposits/{oid}/decline'):
                response = await c.post(path, data={'csrf_token': csrf(page), 'reason': 'Synthetic decline'})
                assert response.status_code == 403, response.text
            assert await outcome(engine, oid, users['trader'].id) == before
    asyncio.run(run())

@pytest.mark.parametrize('method', METHODS)
@pytest.mark.parametrize('decision,role', [('approved','admin'), ('rejected','superadmin')])
def test_payment_not_found_is_nonfinancial_admin_review(monkeypatch, tmp_path, method, decision, role):
    import app.web.routes as web
    monkeypatch.setattr(web, 'APPEAL_UPLOAD_ROOT', tmp_path)
    async def run():
        async with isolated(monkeypatch) as (engine, merchant, users, keys), client() as c, client() as staff:
            oid = await method_operation(engine, merchant, users, keys, c, method)
            await token_login(staff, users['merchant']); aid = await open_appeal(staff, oid)
            await login(c, users['trader'], realm='trader')
            page = await c.get('/trader/cabinet/tradespace/disputes')
            assert 'Платёж не найден' in page.text
            before = await outcome(engine, oid, users['trader'].id)
            response = await c.post(f'/trader/cabinet/appeals/{aid}/trader/reject',
                data={'csrf_token': csrf(page), 'reason': 'no_payment'}, files={'statement_file': ('evidence.png', image_png(PAYLOAD), 'image/png')})
            assert response.status_code == 303, response.text
            assert 'ui_error' not in response.headers['location']
            refreshed = await c.get(response.headers['location'], follow_redirects=True)
            assert 'Ожидается проверка администрации.' in refreshed.text and 'Ответ передан' in refreshed.text
            assert await outcome(engine, oid, users['trader'].id) == before
            async with AsyncSession(engine) as db:
                claim = await db.get(m.Appeal, aid)
                assert claim.status == 'in_review' and claim.metadata_json['review_required']
                assert claim.metadata_json['trader_decision_by'] == str(users['trader'].id)
                assert claim.metadata_json['trader_decision_at'] and claim.metadata_json['statement_file']
                messages = list(await db.scalars(select(m.AppealMessage).where(m.AppealMessage.appeal_id == aid)))
                assert any(x.author_id == users['trader'].id and 'Передано администрации' in x.message for x in messages)
                assert await db.scalar(select(func.count()).select_from(m.AuditLog).where(m.AuditLog.action == 'trader_appeal_referred')) == 1
            # Stale form, ordinary confirm and timeout cannot bypass the review.
            await c.post(f'/trader/cabinet/appeals/{aid}/trader/accept', data={'csrf_token': csrf(page)})
            async with AsyncSession(engine) as db:
                with pytest.raises(DepositConfirmationConflict):
                    await confirm_deposit_payment(db, oid, actor_id=users['trader'].id, actor_ip='127.0.0.1', audit_action='test', description='test')
            async with AsyncSession(engine) as db:
                claim = await db.get(m.Appeal, aid)
                claim.metadata_json = {**claim.metadata_json, 'deadline_at': (datetime.now(timezone.utc)-timedelta(days=1)).isoformat()}
                await db.commit()
                assert await appeals.approve_expired_appeals(db) == []
                await db.commit()
            assert await outcome(engine, oid, users['trader'].id) == before
            await token_login(staff, users['support'])
            path = f'/api/v1/admin/appeals/{aid}/resolve'
            assert (await staff.post(path, json={'status': decision, 'decision': 'Synthetic review'})).status_code == 403
            staff.headers.pop('Authorization', None)
            await login(staff, users[role], realm='staff')
            queue = await staff.get('/staff/cabinet/tradespace/operations?tab=appeals&id='+str(aid))
            assert queue.status_code == 200 and str(aid) in queue.text
            await token_login(staff, users[role])
            responses = await asyncio.wait_for(asyncio.gather(*(staff.post(path, json={'status': decision, 'decision': 'Synthetic reviewed evidence'}) for _ in range(2))), 20)
            assert sorted(r.status_code for r in responses) == [200,409], [r.text for r in responses]
            after = await outcome(engine, oid, users['trader'].id)
            if decision == 'approved':
                assert after[:6] == ('paid',False,D('99050'),D('0'),D('900'),D('0'))
                assert sum(t == 'deposit.paid' for _,t in after[6]) == 1
            else:
                assert after[0] == 'pending' and after[1:6] == before[1:6]
                assert after[7:] == before[7:]
                assert not any(t in {'deposit.paid','deposit.failed','deposit.cancelled'} for _,t in after[6])
            assert (await staff.post(path, json={'status': decision, 'decision':'Retry'})).status_code == 409
            assert await outcome(engine, oid, users['trader'].id) == after
    asyncio.run(run())

@pytest.mark.parametrize('method', METHODS)
def test_timeout_is_distinct_from_dispute_escalation(monkeypatch, method):
    async def run():
        async with isolated(monkeypatch) as (engine, merchant, users, keys), client() as c:
            oid = await method_operation(engine, merchant, users, keys, c, method)
            async with AsyncSession(engine) as db:
                dep = await db.get(m.Deposit, oid); dep.expires_at = datetime.now(timezone.utc)-timedelta(seconds=1)
                await db.commit(); await expire_due_deposits(db); await db.commit()
                assert (await db.get(m.Deposit, oid)).status == 'failed'
                assert await db.scalar(select(func.count()).select_from(m.Appeal)) == 0
            # A real claim after expiry escalates; time itself does not create it or pay it.
            await token_login(c, users['merchant']); aid = await open_appeal(c, oid)
            before = await outcome(engine, oid, users['trader'].id)
            async with AsyncSession(engine) as db:
                claim = await db.get(m.Appeal, aid)
                claim.metadata_json = {**claim.metadata_json, 'deadline_at': (datetime.now(timezone.utc)-timedelta(days=1)).isoformat()}
                await db.commit(); await appeals.approve_expired_appeals(db); await db.commit(); await db.refresh(claim)
                assert claim.status == 'in_review' and claim.metadata_json['review_reason'] == 'response_timeout'
            assert await outcome(engine, oid, users['trader'].id) == before
    asyncio.run(run())

def test_foreign_trader_and_racing_opposite_decisions(monkeypatch):
    async def run():
        async with isolated(monkeypatch) as (engine, merchant, users, keys), client() as c:
            oid = await method_operation(engine, merchant, users, keys, c, 'sbp_qr')
            await token_login(c, users['merchant']); aid = await open_appeal(c, oid)
            async with AsyncSession(engine) as db:
                foreign = await db.get(m.User, users['aggregator'].id); foreign.role = 'trader'; await db.commit()
            async with client() as foreign_client:
                await login(foreign_client, users['aggregator'], realm='trader')
                page = await foreign_client.get('/trader/cabinet/tradespace/disputes')
                assert str(aid) not in page.text
                before = await outcome(engine, oid, users['trader'].id)
                await foreign_client.post(f'/trader/cabinet/appeals/{aid}/trader/accept', data={'csrf_token':csrf(page)})
                assert await outcome(engine, oid, users['trader'].id) == before
            async def command(name, actor):
                async with AsyncSession(engine) as db:
                    appeal = await db.get(m.Appeal, aid)
                    try:
                        if name == 'refer': await appeals.refer_appeal_to_staff(db, appeal, actor_id=actor, response={'trader_rejection_reason':'wrong_requisite'})
                        elif name == 'approve': await appeals.approve_deposit_appeal(db, appeal, actor_id=actor)
                        else: await appeals.reject_deposit_appeal(db, appeal, actor_id=actor)
                        await db.commit(); return name
                    except ValueError:
                        await db.rollback(); return 'conflict'
            results = await asyncio.wait_for(asyncio.gather(command('refer',users['trader'].id), command('approve',users['superadmin'].id),command('reject',users['admin'].id)),20)
            assert sum(x in {'approve','reject'} for x in results) == 1, results
            after = await outcome(engine, oid, users['trader'].id)
            assert sum(t=='deposit.paid' for _,t in after[6]) == (1 if 'approve' in results else 0)
            assert after[0] == ('paid' if 'approve' in results else 'pending')
    asyncio.run(run())

def test_qr_tariff_ui_save_reload_and_immutable_snapshot(monkeypatch):
    async def run():
        async with isolated(monkeypatch) as (engine, merchant, users, keys), client() as c:
            await prepare(engine, merchant, users, 'merchant')
            async with AsyncSession(engine) as db:
                db.add(m.FeeRule(entity_type='merchant', entity_id=merchant.id, fee_side='merchant_fee',method='sbp',payment_method='sbp',currency='RUB',min_amount=D('0'),percent=D('7'),rate_percent=D('7'),effective_from=datetime.now(timezone.utc)-timedelta(days=1)))
                await db.commit()
            old,_ = result(await create(c,'merchant',keys,(None,None)), 'merchant')
            await login(c, users['superadmin'], realm='staff')
            url='/staff/cabinet/tradespace/network?tab=merchants&id='+str(merchant.id)
            page=await c.get(url);assert 'Тарифы · QR NSPK' in page.text
            response=await c.post(f'/staff/cabinet/merchants/{merchant.id}/commission-tiers', data={'csrf_token':csrf(page),'payment_method':'sbp_qr',**{t['field']:'12.00' for t in COMMISSION_TIER_RANGES}})
            assert response.status_code==303,response.text
            returned=await c.get(response.headers['location'],follow_redirects=True)
            assert returned.status_code==200 and 'Тарифы · QR NSPK' in returned.text
            again=await c.get(url);assert 'value="12.00"' in again.text or 'value="12.0000"' in again.text
            new,_ = result(await create(c,'merchant',keys,(None,None),amount='1001'), 'merchant')
            async with AsyncSession(engine) as db:
                first=await db.scalar(select(m.OperationFeeSnapshot).where(m.OperationFeeSnapshot.deposit_id==old))
                second=await db.scalar(select(m.OperationFeeSnapshot).where(m.OperationFeeSnapshot.deposit_id==new))
                assert first.merchant_fee_amount==D('100') and second.merchant_fee_amount==D('120.12')
                sbp=await db.scalar(select(m.FeeRule).where(m.FeeRule.entity_id==merchant.id,m.FeeRule.payment_method=='sbp'))
                assert sbp.rate_percent==D('7') and sbp.is_active
    asyncio.run(run())

def test_qr_edit_without_upload_preserves_identity(monkeypatch):
    async def run():
        async with isolated(monkeypatch) as (engine, merchant, users, keys),client() as c:
            await prepare(engine,merchant,users,'merchant')
            async with AsyncSession(engine) as db:
                req=await db.scalar(select(m.Requisite)); rid=req.id; original=(reveal_text(req.value_encrypted),req.qr_identity)
            await login(c,users['trader'],realm='trader')
            page=await c.get('/trader/cabinet/tradespace/requisites')
            response=await c.post(f'/trader/cabinet/requisites/{rid}/edit',data={'csrf_token':csrf(page),'method':'sbp_qr','full_name':'Synthetic edited','bank_code':'sber','bank_name':'СберБанк','simultaneous_limit':'10'})
            assert response.status_code==303 and 'ui_error' not in response.headers['location'],response.text
            async with AsyncSession(engine) as db:
                req=await db.get(m.Requisite,rid);assert req.method=='sbp_qr' and (reveal_text(req.value_encrypted),req.qr_identity)==original
    asyncio.run(run())

@pytest.mark.parametrize('kind', ['valid','expired','none','wrong_algorithm','tampered','malformed','deep_json'])
def test_patched_jwt_preserves_auth_contract(kind):
    from app.core.security import create_token, decode_token
    from app.core.config import settings
    if kind=='valid':
        token=create_token('synthetic-user','access',timedelta(minutes=5),{'realm':'trader'})
        data=decode_token(token);assert data['sub']=='synthetic-user' and data['realm']=='trader';return
    if kind=='expired':token=create_token('synthetic-user','access',timedelta(seconds=-10))
    elif kind=='none':token=jwt.encode({'sub':'synthetic-user'},key=None,algorithm='none')
    elif kind=='wrong_algorithm':token=jwt.encode({'sub':'synthetic-user'},settings.SECRET_KEY,algorithm='HS512')
    elif kind=='tampered':token=create_token('synthetic-user','access',timedelta(minutes=5))[:-10]+'AAAAAAAAAA'
    elif kind=='malformed':token='not.a.jwt'
    else:
        # Maintainer CVE-2026-101918: deeply nested JSON must produce invalid token, not RecursionError/500.
        token=base64.urlsafe_b64encode(('['*2000+'0'+']'*2000).encode()).decode().rstrip('=')+'.e30.AAAA'
    with pytest.raises(ValueError,match='invalid token'):decode_token(token)
