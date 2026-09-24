"""Acceptance probes: real API creation and concurrent financial commands.

Disposable PostgreSQL only. Known discrepancies are asserted as observations,
not accepted as desired behavior. Golden files and production code stay intact.
"""
import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.security import encrypt_secret, hash_password
from app.core.config import settings
import app.api.deps as auth_deps
from app.db.session import engine as application_engine
from app.main import app
from app.models import Appeal, Balance, Deposit, FeeRule, OperationFeeSnapshot, Requisite, TraderLedgerEntry, User, WebhookEvent
from app.services.appeals import approve_deposit_appeal, reject_deposit_appeal
from app.services.deposit_confirmation import confirm_deposit_payment, DepositConfirmationConflict
from app.services.deposit_lifecycle import expire_due_deposits, finalize_unsuccessful_deposit
from tests.test_merchant_hmac_v2_postgres import _setup_merchant, _signed_headers, _payload

PATH = '/api/v1/merchant/deposits'


@pytest.fixture(autouse=True)
def replay_client_per_event_loop(monkeypatch):
    # Each asyncio.run owns a different loop. Use a fresh REAL Redis client,
    # preserving nonce/replay enforcement instead of stubbing authentication.
    monkeypatch.setattr(auth_deps, '_merchant_replay_redis', Redis.from_url(settings.REDIS_URL, decode_responses=True))


async def finish(engine):
    await auth_deps._merchant_replay_redis.aclose()
    await engine.dispose()
    await application_engine.dispose()


async def setup(*, balance='100000', enabled=True):
    assert os.environ['ENV']=='test'
    assert os.environ['TEST_DATABASE_URL'].endswith('/tradespace_test')
    await application_engine.dispose(close=False)
    key, secret, mid = await _setup_merchant()
    engine = create_async_engine(os.environ['TEST_DATABASE_URL'])
    async with AsyncSession(engine, expire_on_commit=False) as db:
        trader = User(email=f'acceptance-{uuid.uuid4().hex}@example.test', password_hash=hash_password('acceptance-synthetic-only'), role='trader', trader_balance=Decimal(balance), trader_hold=Decimal('0'), trader_traffic_status='active')
        db.add(trader); await db.flush()
        req = Requisite(trader_id=trader.id, owner_name='Synthetic audit', full_name='Synthetic audit', method='sbp', bank_code='sberbank', bank_name='СберБанк', value_encrypted=encrypt_secret('+79990001122'), enabled=enabled, status='active', min_check=Decimal('1'), max_check=Decimal('100000'), simultaneous_limit=10, daily_limit=Decimal('1000000'), request_count=100, operation_limit=100)
        db.add(req)
        for kind, identifier, side, rate in [('merchant',mid,'merchant_fee','10'),('trader',trader.id,'executor_fee','5')]:
            db.add(FeeRule(entity_type=kind, entity_id=identifier, fee_side=side, method='sbp', payment_method='sbp', currency='RUB', min_amount=Decimal('0'), percent=Decimal(rate), rate_percent=Decimal(rate), effective_from=datetime.now(timezone.utc)-timedelta(days=1)))
        await db.commit()
        return engine, key, secret, mid, trader.id


async def request(key, secret, external, *, cancel=False, amount='1000'):
    path = f'{PATH}/{external}/cancel' if cancel else PATH
    body = b'' if cancel else _payload(external,amount=amount)
    headers = _signed_headers(api_key=key,secret=secret,body=body,nonce=uuid.uuid4().hex,idempotency_key=external,signed_path=path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,raise_app_exceptions=False),base_url='https://localhost') as client:
        return await client.post(path,content=body,headers=headers)


async def state(engine, mid, tid):
    async with AsyncSession(engine) as db:
        trader=await db.get(User,tid)
        bal=(await db.execute(select(Balance).where(Balance.merchant_id==mid))).scalar_one()
        deps=(await db.execute(select(Deposit).where(Deposit.merchant_id==mid))).scalars().all()
        entries=(await db.execute(select(TraderLedgerEntry).where(TraderLedgerEntry.trader_id==tid))).scalars().all()
        events=(await db.execute(select(WebhookEvent).where(WebhookEvent.merchant_id==mid))).scalars().all()
        return {'balance':trader.trader_balance,'hold':trader.trader_hold,'merchant':bal.available,'frozen':bal.frozen,'deposits':[(d.id,d.status) for d in deps],'entries':[(e.entry_type,e.amount) for e in entries],'events':[(e.event_type,e.payload.get('status')) for e in events]}


def test_acceptance_api_replay_then_repeat_cancel_releases_hold_once():
    async def run():
        engine,key,secret,mid,tid=await setup()
        try:
            ext=uuid.uuid4().hex
            a=await request(key,secret,ext); assert a.status_code==200,a.text
            b=await request(key,secret,ext); assert b.status_code==200,b.text
            assert a.json()['id']==b.json()['id'] and b.json()['idempotent']
            before=await state(engine,mid,tid)
            assert before['hold']==Decimal('950') and before['entries']==[('hold',Decimal('950'))]
            for _ in range(2):
                r=await request(key,secret,ext,cancel=True);assert r.status_code==200,r.text
            after=await state(engine,mid,tid)
            assert after['hold']==0 and after['balance']==100000 and after['merchant']==1000
            assert after['entries'].count(('release_hold',Decimal('950')))==1
            assert after['events']==[('deposit.cancelled','cancelled')]
            assert after['deposits'][0][1]=='cancelled'
        finally: await finish(engine)
    asyncio.run(run())


def test_acceptance_unavailable_route_has_no_orphan_hold():
    async def run():
        engine,key,secret,mid,tid=await setup(enabled=False)
        try:
            r=await request(key,secret,uuid.uuid4().hex);assert r.status_code==409,r.text
            result=await state(engine,mid,tid)
            assert result['hold']==0 and result['balance']==100000
            assert result['deposits']==[] and result['entries']==[] and result['events']==[]
        finally: await finish(engine)
    asyncio.run(run())


def test_acceptance_two_api_creates_cannot_reserve_same_collateral_twice():
    async def run():
        engine,key,secret,mid,tid=await setup(balance='1000')
        try:
            responses=await asyncio.wait_for(asyncio.gather(*(request(key,secret,uuid.uuid4().hex) for _ in range(2))),15)
            assert sorted(r.status_code for r in responses)==[200,409],[r.text for r in responses]
            result=await state(engine,mid,tid)
            assert result['hold']==950 and result['balance']==1000
            assert len(result['deposits'])==1 and result['entries']==[('hold',Decimal('950'))]
        finally: await finish(engine)
    asyncio.run(run())


@pytest.mark.parametrize('opposing',['confirm','cancel','expire'])
def test_acceptance_confirmation_race_has_single_financial_outcome(opposing):
    async def run():
        engine,key,secret,mid,tid=await setup()
        try:
            ext=uuid.uuid4().hex;r=await request(key,secret,ext);assert r.status_code==200,r.text
            oid=uuid.UUID(r.json()['id'])
            async def confirm():
                async with AsyncSession(engine,expire_on_commit=False) as db:
                    try: return await confirm_deposit_payment(db,oid,actor_id=tid,actor_ip='127.0.0.1',audit_action='acceptance_confirm',description='Synthetic acceptance')
                    except DepositConfirmationConflict: return 'conflict'
            async def other():
                if opposing=='confirm':return await confirm()
                async with AsyncSession(engine,expire_on_commit=False) as db:
                    if opposing=='expire':
                        await expire_due_deposits(db,now=datetime.now(timezone.utc)+timedelta(days=1))
                    else:
                        dep=(await db.execute(select(Deposit).where(Deposit.id==oid).with_for_update())).scalar_one()
                        if dep.status!='paid':
                            await finalize_unsuccessful_deposit(db,dep,reason='merchant_cancelled',target_status='cancelled')
                    await db.commit()
            await asyncio.wait_for(asyncio.gather(confirm(),other()),15)
            result=await state(engine,mid,tid)
            status=result['deposits'][0][1];assert status in ('paid','cancelled','failed')
            assert result['hold']==0 and len(result['events'])==1
            if status=='paid':
                assert result['balance']==99050 and result['merchant']==1900
                assert result['entries'].count(('deposit_success_debit',Decimal('950')))==1
            else:
                assert result['balance']==100000 and result['merchant']==1000
                assert result['entries'].count(('release_hold',Decimal('950')))==1
            if opposing=='confirm':assert status=='paid'
        finally: await finish(engine)
    asyncio.run(run())


def test_acceptance_inconsistent_active_hold_fails_closed_on_repeat_expiry_and_cancel():
    async def run():
        engine,key,secret,mid,tid=await setup()
        try:
            ext=uuid.uuid4().hex;r=await request(key,secret,ext);assert r.status_code==200,r.text
            oid=uuid.UUID(r.json()['id'])
            # Deliberate test-only fault injection: never applied to dev records.
            async with AsyncSession(engine) as db:
                t=await db.get(User,tid);t.trader_hold=Decimal('0')
                d=await db.get(Deposit,oid);d.expires_at=datetime.now(timezone.utc)-timedelta(minutes=1)
                await db.commit()
            before=await state(engine,mid,tid)
            for _ in range(2):
                async with AsyncSession(engine) as db:
                    assert await expire_due_deposits(db)==[]
                    await db.commit()
                response=await request(key,secret,ext,cancel=True)
                assert response.status_code==409
                assert response.json()['error']['code']=='deposit_reserve_inconsistent'
                assert await state(engine,mid,tid)==before
            assert before['deposits'][0][1]=='pending' and before['events']==[]
        finally: await finish(engine)
    asyncio.run(run())


@pytest.mark.parametrize('decision',['approve','reject'])
def test_acceptance_specialized_appeal_outcome_and_duplicate_resolution(decision):
    async def run():
        engine,key,secret,mid,tid=await setup()
        try:
            ext=uuid.uuid4().hex;r=await request(key,secret,ext);assert r.status_code==200,r.text
            oid=uuid.UUID(r.json()['id'])
            async with AsyncSession(engine,expire_on_commit=False) as db:
                dep=await db.get(Deposit,oid);dep.status='appeal_opened'
                appeal=Appeal(operation_id=oid,operation_type='deposit',created_by=tid,status='opened',metadata_json={'previous_deposit_status':'pending','trader_id':str(tid)})
                db.add(appeal);await db.commit();aid=appeal.id
            async def resolve():
                async with AsyncSession(engine,expire_on_commit=False) as db:
                    ap=await db.get(Appeal,aid)
                    try:
                        fn=approve_deposit_appeal if decision=='approve' else reject_deposit_appeal
                        await fn(db,ap,actor_id=tid);await db.commit();return 'resolved'
                    except ValueError:
                        await db.rollback();return 'already_resolved'
            assert sorted(await asyncio.wait_for(asyncio.gather(resolve(),resolve()),15))==['already_resolved','resolved']
            result=await state(engine,mid,tid)
            if decision=='approve':
                assert result['hold']==0 and result['balance']==99050 and result['merchant']==1900
                assert result['events']==[('deposit.paid','paid')]
                assert result['entries'].count(('deposit_success_debit',Decimal('950')))==1
            else:
                assert result['hold']==950 and result['balance']==100000 and result['merchant']==1000
                assert result['deposits'][0][1]=='pending'
                # Reject restores pending; its event must not report a terminal failure.
                assert result['events']==[('deposit.pending','pending')]
        finally: await finish(engine)
    asyncio.run(run())
