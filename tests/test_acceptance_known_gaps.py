"""Correct-behavior regressions for F06/F08/F10; use real auth and PostgreSQL."""
import asyncio
import json
import logging
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import httpx
import pyotp
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.logging import JsonLogFormatter, RedactingFormatter, protect_log_handlers
from app.core.security import encrypt_secret, hash_password
from app.main import app
from app.models import AggregatorAccount, AggregatorCallbackLog, AggregatorPayment, Appeal, Deposit, User
from app.services.webhooks import WebhookHttpResult
import app.services.aggregators as aggregators
from tests.test_acceptance_financial_postgres import setup, request, state, finish, replay_client_per_event_loop

PASSWORD='synthetic-regression-password'
OTP='JBSWY3DPEHPK3PXP'

@asynccontextmanager
async def authenticated(engine, role='admin'):
    async with AsyncSession(engine,expire_on_commit=False) as db:
        user=User(email=f'fix-{uuid.uuid4().hex}@example.test',password_hash=hash_password(PASSWORD),role=role,
                  twofa_enabled=role in {'admin','superadmin','support','teamlead'},twofa_secret=encrypt_secret(OTP))
        db.add(user);await db.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,raise_app_exceptions=False),base_url='https://localhost') as client:
        response=await client.post('/api/v1/auth/login',json={'email':user.email,'password':PASSWORD,'otp':pyotp.TOTP(OTP).now()})
        assert response.status_code==200,response.text
        client.headers['Authorization']='Bearer '+response.json()['access_token']
        yield client,user

@pytest.mark.parametrize('decision',['approved','rejected','returned_to_processing','closed'])
def test_generic_appeal_http_finance_duplicate_and_concurrency(decision):
    async def run():
        engine,key,secret,mid,tid=await setup()
        try:
            result=await request(key,secret,uuid.uuid4().hex);assert result.status_code==200
            oid=result.json()['id']
            async with authenticated(engine) as (client,actor):
                opened=await client.post('/api/v1/cabinet/appeals',json={'operation_type':'deposit','operation_id':oid,'message':'Synthetic evidence'})
                assert opened.status_code==200,opened.text
                aid=opened.json()['id'];path=f'/api/v1/admin/appeals/{aid}/resolve';data={'status':decision,'decision':'Synthetic decision'}
                responses=await asyncio.wait_for(asyncio.gather(client.post(path,json=data),client.post(path,json=data)),15)
                assert sorted(r.status_code for r in responses)==[200,409],[r.text for r in responses]
                replay=await client.post(path,json=data);assert replay.status_code==409
                missing=await client.post(f'/api/v1/admin/appeals/{uuid.uuid4()}/resolve',json=data);assert missing.status_code==404
            result=await state(engine,mid,tid)
            if decision=='approved':
                assert result['hold']==0 and result['balance']==99050 and result['merchant']==1900
                assert result['events']==[('deposit.paid','paid')]
                assert result['deposits'][0][1]=='paid'
            else:
                assert result['hold']==950 and result['balance']==100000 and result['merchant']==1000
                assert result['events']==[('deposit.pending','pending')]
                assert result['deposits'][0][1]=='pending'
            async with AsyncSession(engine) as db:
                appeal=await db.get(Appeal,uuid.UUID(aid));assert appeal.status==decision
        finally:await finish(engine)
    asyncio.run(run())


def test_generic_appeal_http_rolls_back_invalid_hold_and_enforces_roles():
    async def run():
        engine,key,secret,mid,tid=await setup()
        try:
            result=await request(key,secret,uuid.uuid4().hex);oid=result.json()['id']
            async with authenticated(engine) as (client,actor):
                opened=await client.post('/api/v1/cabinet/appeals',json={'operation_type':'deposit','operation_id':oid,'message':'Synthetic evidence'})
                aid=opened.json()['id'];path=f'/api/v1/admin/appeals/{aid}/resolve';data={'status':'approved','decision':'Synthetic approve'}
                async with AsyncSession(engine) as db:
                    trader=await db.get(User,tid);trader.trader_hold=0;await db.commit() # explicit isolated fault injection
                before=await state(engine,mid,tid)
                response=await client.post(path,json=data);assert response.status_code==409,response.text
                assert await state(engine,mid,tid)==before
            for role in ['support','teamlead','trader','merchant','operator']:
                async with authenticated(engine,role) as (c,actor):
                    denied=await c.post(path,json=data);assert denied.status_code==403,(role,denied.text)
                    if role in {'trader','operator'}:
                        foreign=await c.post('/api/v1/cabinet/appeals',json={'operation_type':'deposit','operation_id':oid,'message':'Foreign target'})
                        assert foreign.status_code==404
        finally:await finish(engine)
    asyncio.run(run())

async def callback_fixture(engine,mid,oid):
    tag=uuid.uuid4().hex
    async with AsyncSession(engine,expire_on_commit=False) as db:
        account=AggregatorAccount(platform_merchant_id=mid,name=tag,api_key=tag,secret_hash=encrypt_secret('synthetic-callback-secret'))
        db.add(account);await db.flush()
        payment=AggregatorPayment(aggregator_id=account.id,merchant_order_id=tag,aggregator_order_id=tag,platform_payment_id=oid,amount=1000,currency='RUB',payment_method='sbp',status='pending',expires_at=datetime.now(timezone.utc)+timedelta(hours=1))
        db.add(payment);await db.flush()
        row=AggregatorCallbackLog(direction='platform_to_aggregator',related_payment_id=payment.id,target_url='http://127.0.0.1:8011/callback',payload_json={'status':'pending'},status='pending')
        db.add(row);await db.commit();return row.id

@pytest.mark.parametrize('failure',['http500','transport','stale_claim'])
def test_aggregator_callback_result_retry_and_recovery(monkeypatch,failure):
    calls=[]
    async def transport(*args,**kwargs):
        calls.append(kwargs)
        if len(calls)==1 and failure=='http500':return WebhookHttpResult(500)
        if len(calls)==1 and failure=='transport':raise OSError('synthetic connection interrupted')
        return WebhookHttpResult(204)
    monkeypatch.setattr(aggregators,'_post_validated_webhook',transport)
    async def run():
        engine,key,secret,mid,tid=await setup()
        try:
            r=await request(key,secret,uuid.uuid4().hex);oid=uuid.UUID(r.json()['id']);before=await state(engine,mid,tid)
            lid=await callback_fixture(engine,mid,oid)
            if failure=='stale_claim':
                async with AsyncSession(engine) as db:
                    row=await db.get(AggregatorCallbackLog,lid);row.status='delivering';row.attempt=1;row.next_retry_at=datetime.now(timezone.utc)-timedelta(minutes=5);await db.commit()
            async with AsyncSession(engine,expire_on_commit=False) as db:
                row=await aggregators.deliver_callback_log(db,lid);await db.commit()
                if failure!='stale_claim':
                    assert row.status=='pending' and row.error_message and row.next_retry_at
                    assert row.response_status_code==(500 if failure=='http500' else None)
                    await aggregators.deliver_callback_log(db,lid);assert len(calls)==1
                    row.next_retry_at=datetime.now(timezone.utc)-timedelta(minutes=1);await db.commit()
                    row=await aggregators.deliver_callback_log(db,lid);await db.commit()
                assert row.status=='sent' and row.response_status_code==204 and row.sent_at and row.error_message is None
                attempts=row.attempt;await aggregators.deliver_callback_log(db,lid);await db.commit();assert row.attempt==attempts
            assert await state(engine,mid,tid)==before
        finally:await finish(engine)
    asyncio.run(run())

@pytest.mark.parametrize('format_kind',['json','text','handler'])
def test_exception_and_nested_secret_redaction(format_kind):
    canaries=['canary-password-123456','canary-token-123456','canary-key-123456']
    try:
        try:raise ValueError('password='+canaries[0])
        except ValueError as cause:raise RuntimeError('token: "'+canaries[1]+'"') from cause
    except RuntimeError:
        record=logging.LogRecord('remediation',logging.ERROR,__file__,1,'api_key='+canaries[2],(),sys.exc_info())
    record.nested={'authorization':'Bearer '+canaries[1],'items':[{'password':canaries[0]}]}
    record.stack_info='signature='+canaries[2]+' postgresql://synthetic:'+canaries[0]+'@localhost/db'
    formatter=JsonLogFormatter() if format_kind=='json' else RedactingFormatter(logging.Formatter('%(message)s %(nested)s'))
    if format_kind=='handler':
        import io
        stream=io.StringIO();handler=logging.StreamHandler(stream);handler.setFormatter(logging.Formatter('%(message)s %(nested)s'))
        logger=logging.getLogger('remediation.canary');logger.handlers=[handler];logger.propagate=False;protect_log_handlers();logger.handle(record);value=stream.getvalue();logger.handlers=[]
    else:value=formatter.format(record)
    assert all(canary not in value for canary in canaries)
    assert 'RuntimeError' in value and 'ValueError' in value and 'redacted' in value

@pytest.mark.parametrize('decision',['approved','rejected'])
def test_generic_payout_appeal_http_financial_resolution(decision):
    from app.models import Balance,LedgerEntry,Payout,WebhookEvent
    from tests.test_merchant_hmac_v2_postgres import _payload,_signed_headers
    async def run():
        engine,key,secret,mid,_=await setup()
        try:
            external=uuid.uuid4().hex;body=_payload(external,amount='100')
            headers=_signed_headers(api_key=key,secret=secret,body=body,nonce=uuid.uuid4().hex,idempotency_key=external)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://localhost') as c:
                response=await c.post('/api/v1/merchant/payouts',content=body,headers=headers)
                assert response.status_code==200,response.text
            oid=uuid.UUID(response.json()['id'])
            async with authenticated(engine,'superadmin') as (client,actor):
                response=await client.post('/api/v1/cabinet/appeals',json={'operation_type':'payout','operation_id':str(oid),'message':'Synthetic payout evidence'})
                assert response.status_code==200,response.text
                path='/api/v1/admin/appeals/'+response.json()['id']+'/resolve';data={'status':decision,'decision':'Synthetic payout decision'}
                results=await asyncio.wait_for(asyncio.gather(client.post(path,json=data),client.post(path,json=data)),15)
                assert sorted(r.status_code for r in results)==[200,409],[r.text for r in results]
                assert (await client.post(path,json=data)).status_code==409
            async with AsyncSession(engine) as db:
                payout=await db.get(Payout,oid);balance=await db.scalar(select(Balance).where(Balance.merchant_id==mid))
                assert payout.status==('completed' if decision=='approved' else 'pending')
                assert balance.available==900 and balance.frozen==(0 if decision=='approved' else 100)
                rows=(await db.scalars(select(LedgerEntry).where(LedgerEntry.operation_id==oid))).all()
                assert sorted(e.entry_type for e in rows)==(['debit','hold'] if decision=='approved' else ['hold'])
                events=(await db.scalars(select(WebhookEvent).where(WebhookEvent.merchant_id==mid))).all()
                assert [(e.event_type,e.payload['status']) for e in events]==([('payout.completed','completed')] if decision=='approved' else [])
        finally:await finish(engine)
    asyncio.run(run())
