"""QR-1: synthetic payloads only; PostgreSQL schemas isolated per scenario."""
import asyncio
from datetime import datetime,timedelta,timezone
from decimal import Decimal as D
from io import BytesIO
import json
import time
import uuid
import pytest
from PIL import Image
from sqlalchemy import select,func
from sqlalchemy.ext.asyncio import AsyncSession
from app import models as m
from app.core.security import encrypt_secret,sign_hmac
from app.services.payment_qr import validate_payload,decode_image,image_png,QrValidationError
from app.services.deposit_confirmation import confirm_deposit_payment,DepositConfirmationConflict
from app.services.deposit_lifecycle import finalize_unsuccessful_deposit,expire_due_deposits
from tests.test_appeal_confirmation_rule_postgres import isolated
from tests.test_integration_modes_postgres import client,token_login
from tests.test_merchant_hmac_v2_postgres import _signed_headers
from tests.test_aggregator_credentials_postgres import seed,issue,API
from tests.tradespace.test_wave4_teamlead import login,csrf

# Deliberately synthetic registration ID; never pay this test QR.
PAYLOAD='https://qr.nspk.ru/AS100000000000000000000000000000?type=01&bank=100000000111&crc=ABCD'

@pytest.mark.parametrize('fmt,side',[('PNG',400),('PNG',900),('JPEG',600)])
def test_image_identity_independent_of_encoding(fmt,side):
    im=Image.open(BytesIO(image_png(PAYLOAD))).convert('RGB').resize((side,side))
    out=BytesIO();im.save(out,format=fmt)
    assert decode_image(out.getvalue()).identity==validate_payload(PAYLOAD).identity

@pytest.mark.parametrize('bad',[
 PAYLOAD.replace('https:','http:'),PAYLOAD.replace('qr.nspk.ru','evil.example'),
 PAYLOAD.replace('type=01','type=02'),PAYLOAD+'&sum=0',PAYLOAD+'&sum=100',
 PAYLOAD+'&type=01',PAYLOAD+'&next=https://example.test',PAYLOAD+'&cur=USD',
 PAYLOAD.replace('ABCD','zzzz'),PAYLOAD.split('?')[0],PAYLOAD+'#fragment',
 PAYLOAD.replace('bank=100000000111','bank=1'),PAYLOAD.replace('type=01','type=%30%31')])
def test_unsupported_payloads_rejected(bad):
    with pytest.raises(QrValidationError):validate_payload(bad)

@pytest.mark.parametrize('kind',['random','oversize','tiny','huge','gif','blank','multi'])
def test_bad_uploads(kind):
    if kind=='random':content=b'not an image'
    elif kind=='oversize':content=b'x'*1_000_001
    else:
        size={'tiny':(32,32),'huge':(4097,64),'multi':(1200,600)}.get(kind,(400,400))
        im=Image.new('RGB',size,'white')
        if kind=='multi':
            qr=Image.open(BytesIO(image_png(PAYLOAD))).resize((500,500))
            im.paste(qr,(0,0));im.paste(qr,(650,0))
        out=BytesIO();im.save(out,format='GIF' if kind=='gif' else 'PNG');content=out.getvalue()
    with pytest.raises(QrValidationError):decode_image(content)


def test_qr_is_collection_only_and_capability_redacted():
    from app.schemas.common import PayoutCreate
    from app.core.logging import redact_sensitive
    with pytest.raises(ValueError):PayoutCreate(external_id='test',amount='100',method='sbp_qr',destination='test')
    assert 'x'*43 not in redact_sensitive('/pay/qr/'+'x'*43+'/image')


async def prepare(engine,merchant,users,origin):
    account=None;key=None;secret=None
    if origin=='aggregator':
        account=await seed(engine,merchant)
        key,secret=await issue(engine,account,users['superadmin'],'sandbox')
    async with AsyncSession(engine) as db:
        req=await db.scalar(select(m.Requisite))
        req.method='sbp_qr';req.value_encrypted=encrypt_secret(PAYLOAD);req.qr_identity=validate_payload(PAYLOAD).identity
        for rule in await db.scalars(select(m.FeeRule)):
            rule.method=rule.payment_method='sbp_qr'
        await db.commit()
    return key,secret

async def create(c,origin,keys,agg,*,tag=None,amount='1000',compact=True):
    tag=tag or uuid.uuid4().hex
    if origin=='merchant':
        path='/api/v1/merchant/deposits';key,secret=keys['sandbox']
        body=json.dumps(dict(external_id=tag,amount=amount,currency='RUB',method='sbp_qr'),separators=(',',':')).encode()
        headers=_signed_headers(api_key=key,secret=secret,body=body,nonce=uuid.uuid4().hex,idempotency_key=tag,signed_path=path)
    else:
        path=API;key,secret=agg;ts=str(int(time.time()))
        body=json.dumps(dict(aggregator_order_id=tag,merchant_order_id=tag,external_merchant_id='synthetic',amount=amount,currency='RUB',payment_method='sbp_qr'),separators=(',',':') if compact else (', ', ': ')).encode()
        headers={'Content-Type':'application/json','X-API-Key':key.api_key,'X-Timestamp':ts,'X-Signature':sign_hmac(secret,ts,body),'X-Request-ID':uuid.uuid4().hex,'X-Idempotency-Key':tag}
    return await c.post(path,content=body,headers=headers)

def result(r,origin):
    assert r.status_code==200,r.text
    data=r.json()
    return uuid.UUID(data['id'] if origin=='merchant' else data['platform_payment_id']), data['payment_details']['qr'] if origin=='merchant' else data['qr']

async def confirm(engine,oid,trader):
    async with AsyncSession(engine,expire_on_commit=False) as db:
        return await confirm_deposit_payment(db,oid,actor_id=trader.id,actor_ip='127.0.0.1',audit_action='qr_test_manual',description='Synthetic QR receipt')

@pytest.mark.parametrize('origin',['merchant','aggregator'])
def test_both_api_snapshot_repeat_manual_finance_webhook_retry(monkeypatch,origin):
    async def run():
        async with isolated(monkeypatch) as (engine,merchant,users,keys),client() as c:
            agg=await prepare(engine,merchant,users,origin);tag=uuid.uuid4().hex
            oid,qr=result(await create(c,origin,keys,agg,tag=tag),origin)
            assert qr['type']=='static_open_amount' and qr['payment_url']==PAYLOAD
            # HTTP GET only, no lifecycle mutation; image is the issued payload.
            page=await c.get(qr['checkout_url']);assert page.status_code==200 and '1000.00' in page.text
            image=await c.get(qr['image_url']);assert image.status_code==200 and image.headers['cache-control']=='no-store'
            assert decode_image(image.content).payload==PAYLOAD
            assert (await c.get('/pay/qr/'+str(oid))).status_code==404
            async with AsyncSession(engine) as db:
                req=await db.scalar(select(m.Requisite));req.value_encrypted=encrypt_secret(PAYLOAD.replace('AS1','AS2'));req.qr_identity=validate_payload(PAYLOAD.replace('AS1','AS2')).identity;req.is_archived=True
                await db.commit()
            if origin=='aggregator':
                # Preserve the existing byte-body replay contract. A newly signed
                # semantically identical request is idempotent without touching DB nonces.
                duplicate=await create(c,origin,keys,agg,tag=tag)
                assert duplicate.status_code==409 and 'hmac replay detected' in duplicate.text
                ts=str(int(time.time()));key,secret=agg
                headers={'X-API-Key':key.api_key,'X-Timestamp':ts,'X-Signature':sign_hmac(secret,ts,b''),'X-Request-ID':uuid.uuid4().hex}
                status=await c.get(API+'/'+str(oid),headers=headers)
                assert status.status_code==200 and status.json()['qr']==qr
                assert status.json()['payment_method']=='sbp_qr'
            retry_id,retry_qr=result(await create(c,origin,keys,agg,tag=tag,compact=False),origin)
            assert retry_id==oid and retry_qr==qr
            async with AsyncSession(engine) as db:
                trader=await db.get(m.User,users['trader'].id)
                assert trader.trader_hold==D('950' if origin=='merchant' else '1000')
                assert await db.scalar(select(func.count()).select_from(m.Deposit))==1
            # Parallel confirmations have exactly one monetary outcome.
            outcomes=await asyncio.gather(confirm(engine,oid,users['trader']),confirm(engine,oid,users['trader']),return_exceptions=True)
            assert sum(not isinstance(x,Exception) for x in outcomes)==1,outcomes
            assert any(isinstance(x,DepositConfirmationConflict) for x in outcomes)
            async with AsyncSession(engine,expire_on_commit=False) as db:
                dep=await db.get(m.Deposit,oid);trader=await db.get(m.User,users['trader'].id)
                assert dep.status=='paid' and not dep.qr_reserved
                assert trader.trader_hold==0 and trader.trader_balance==D('99050' if origin=='merchant' else '99000')
                bal=await db.scalar(select(m.Balance).where(m.Balance.merchant_id==merchant.id));assert bal.available==900
                fee=await db.scalar(select(m.OperationFeeSnapshot).where(m.OperationFeeSnapshot.deposit_id==oid));assert (fee.merchant_fee_amount,fee.executor_fee_amount,fee.platform_income_amount)==(100,50,50)
                event=await db.scalar(select(m.WebhookEvent).where(m.WebhookEvent.event_type=='deposit.paid'));assert event
                from app.services.webhooks import deliver_event,WebhookHttpResult
                async def failed(*args,**kwargs):return WebhookHttpResult(500)
                async def success(*args,**kwargs):return WebhookHttpResult(204)
                before=(trader.trader_balance,bal.available)
                one=await deliver_event(db,event.id,http_post=failed);await db.commit();assert one.status=='retry_scheduled'
                event.next_attempt_at=datetime.now(timezone.utc)-timedelta(seconds=1);await db.commit()
                two=await deliver_event(db,event.id,http_post=success);await db.commit();assert two.status=='delivered'
                await db.refresh(trader);await db.refresh(bal);assert before==(trader.trader_balance,bal.available)
                if origin=='aggregator':
                    p=await db.scalar(select(m.AggregatorPayment).where(m.AggregatorPayment.platform_payment_id==oid));assert p.status=='paid'
                    from app.services import aggregators
                    callback=await db.scalar(select(m.AggregatorCallbackLog).where(m.AggregatorCallbackLog.related_payment_id==p.id))
                    assert callback
                    captured=[]
                    async def callback_http(url,body,headers,**kwargs):
                        assert headers['X-Signature']==sign_hmac(agg[1],headers['X-Timestamp'],body)
                        assert json.loads(body)['status']=='paid'
                        captured.append(headers)
                        return WebhookHttpResult(500 if len(captured)==1 else 204)
                    monkeypatch.setattr(aggregators,'_post_validated_webhook',callback_http)
                    first=await aggregators.deliver_callback_log(db,callback.id);await db.commit()
                    assert first.status=='pending' and first.attempt==1
                    first.next_retry_at=datetime.now(timezone.utc)-timedelta(seconds=1);await db.commit()
                    second=await aggregators.deliver_callback_log(db,callback.id);await db.commit()
                    assert second.status=='sent' and second.attempt==2
                    await aggregators.deliver_callback_log(db,callback.id)
                    assert len(captured)==2 and captured[0]['X-Idempotency-Key']==captured[1]['X-Idempotency-Key']
                    await db.refresh(trader);await db.refresh(bal);assert before==(trader.trader_balance,bal.available)
            assert (await c.get(qr['image_url'])).status_code==410
    asyncio.run(run())


def test_cross_api_parallel_same_qr_fallback_and_distinct_amounts(monkeypatch):
    async def run():
        async with isolated(monkeypatch) as (engine,merchant,users,keys),client() as c:
            agg=await prepare(engine,merchant,users,'aggregator')
            async with AsyncSession(engine) as db:
                req=await db.scalar(select(m.Requisite))
                db.add(m.Requisite(trader_id=users['trader'].id,owner_name='Duplicate image',method='sbp_qr',qr_identity=req.qr_identity,
                    value_encrypted=encrypt_secret(PAYLOAD),bank_code='sberbank',bank_name='СберБанк',min_check=1,max_check=100000,simultaneous_limit=20))
                await db.commit()
            responses=await asyncio.wait_for(asyncio.gather(create(c,'merchant',keys,agg),create(c,'aggregator',keys,agg)),20)
            assert sorted(r.status_code for r in responses)==[200,409],[r.text for r in responses]
            result(await create(c,'merchant',keys,agg,amount='1001'),'merchant')
            # A second eligible trader with a different QR is selected on collision.
            async with AsyncSession(engine) as db:
                users['other']=m.User(email=uuid.uuid4().hex+'@example.test',role='trader',password_hash='not-used',trader_balance=D('100000'))
                db.add(users['other']);await db.flush()
                db.add(m.FeeRule(entity_type='trader',entity_id=users['other'].id,fee_side='executor_fee',method='sbp_qr',payment_method='sbp_qr',currency='RUB',min_amount=0,percent=5,rate_percent=5,effective_from=datetime.now(timezone.utc)-timedelta(days=1)))
                other=PAYLOAD.replace('AS1','AS2');db.add(m.Requisite(trader_id=users['other'].id,owner_name='Other',method='sbp_qr',qr_identity=validate_payload(other).identity,value_encrypted=encrypt_secret(other),bank_code='sberbank',bank_name='СберБанк',min_check=1,max_check=100000))
                await db.commit()
            oid,qr=result(await create(c,'merchant',keys,agg),'merchant');assert qr['payment_url']==other
            assert (await create(c,'merchant',keys,agg)).status_code==409
    asyncio.run(run())


def test_concurrent_idempotent_create_and_required_fee(monkeypatch):
    async def run():
        async with isolated(monkeypatch) as (engine,merchant,users,keys),client() as c:
            agg=await prepare(engine,merchant,users,'merchant');tag=uuid.uuid4().hex
            responses=await asyncio.gather(create(c,'merchant',keys,agg,tag=tag),create(c,'merchant',keys,agg,tag=tag))
            assert result(responses[0],'merchant')==result(responses[1],'merchant')
            async with AsyncSession(engine) as db:
                trader=await db.get(m.User,users['trader'].id);assert trader.trader_hold==950
                for rule in await db.scalars(select(m.FeeRule).where(m.FeeRule.entity_type=='trader')):rule.is_active=False
                await db.commit()
            denied=await create(c,'merchant',keys,agg,amount='1001');assert denied.status_code==409 and 'fee_rule_missing' in denied.text
    asyncio.run(run())

@pytest.mark.parametrize('origin',['merchant','aggregator'])
@pytest.mark.parametrize('terminal',['failed','cancelled','expired'])
def test_release_and_manual_late_appeal_no_auto_or_sms(monkeypatch,origin,terminal):
    async def run():
        async with isolated(monkeypatch) as (engine,merchant,users,keys),client() as c:
            agg=await prepare(engine,merchant,users,origin);oid,_=result(await create(c,origin,keys,agg),origin)
            async with AsyncSession(engine,expire_on_commit=False) as db:
                dep=await db.get(m.Deposit,oid)
                from app.services.sms import confirm_deposit_from_sms
                with pytest.raises(ValueError,match='QR NSPK'):await confirm_deposit_from_sms(db,uuid.uuid4(),oid)
                await finalize_unsuccessful_deposit(db,dep,reason='synthetic_expiry',target_status=terminal);await db.commit()
                trader=await db.get(m.User,users['trader'].id);assert trader.trader_hold==0 and trader.trader_balance==100000
                assert not dep.qr_reserved
                from tests.test_appeal_confirmation_rule_postgres import open_appeal
                await token_login(c,users['superadmin'])
                aid=await open_appeal(c,oid)
                appeal=await db.get(m.Appeal,aid)
                appeal.created_at=datetime.now(timezone.utc)-timedelta(days=1);await db.commit()
                from app.services.appeals import approve_deposit_appeal,approve_expired_appeals
                with pytest.raises(ValueError,match='ручного решения'):await approve_deposit_appeal(db,appeal,automatic=True)
                assert await approve_expired_appeals(db)==[]
                await approve_deposit_appeal(db,appeal,actor_id=users['superadmin'].id);await db.commit()
                await db.refresh(trader);assert trader.trader_hold==0 and trader.trader_balance==D('99050' if origin=='merchant' else '99000')
                with pytest.raises(ValueError):await approve_deposit_appeal(db,appeal,actor_id=users['superadmin'].id)
    asyncio.run(run())


def test_confirmation_expiry_race(monkeypatch):
    async def run():
        async with isolated(monkeypatch) as (engine,merchant,users,keys),client() as c:
            agg=await prepare(engine,merchant,users,'merchant');oid,qr=result(await create(c,'merchant',keys,agg),'merchant')
            async with AsyncSession(engine) as db:
                dep=await db.get(m.Deposit,oid);dep.expires_at=datetime.now(timezone.utc)-timedelta(seconds=1);await db.commit()
            await login(c,users['trader'],realm='trader')
            page=await c.get('/trader/cabinet/tradespace/operations/'+str(oid))
            assert page.status_code==200 and 'data-dialog-open="confirm-' not in page.text
            assert (await c.get(qr['image_url'])).status_code==410
            async with AsyncSession(engine) as db:
                dep=await db.get(m.Deposit,oid)
                assert dep.status=='pending' and dep.qr_reserved  # reads never expire it
            async def expire():
                async with AsyncSession(engine,expire_on_commit=False) as db:
                    rows=await expire_due_deposits(db);await db.commit();return rows
            await asyncio.gather(confirm(engine,oid,users['trader']),expire(),return_exceptions=True)
            async with AsyncSession(engine) as db:
                dep=await db.get(m.Deposit,oid);trader=await db.get(m.User,users['trader'].id)
                assert dep.status=='failed' and not dep.qr_reserved and trader.trader_hold==0 and trader.trader_balance==100000
                assert await db.scalar(select(func.count()).select_from(m.WebhookEvent).where(m.WebhookEvent.event_type=='deposit.paid'))==0
    asyncio.run(run())


@pytest.mark.parametrize('decision',['confirm','decline'])
def test_browser_upload_ownership_csrf_and_manual_commands(monkeypatch,decision):
    async def run():
        async with isolated(monkeypatch) as (engine,merchant,users,keys),client() as c:
            await prepare(engine,merchant,users,'merchant')
            await login(c,users['trader'],realm='trader')
            page=await c.get('/trader/cabinet/tradespace/requisites');assert page.status_code==200
            fields={'csrf_token':csrf(page),'method':'sbp_qr','full_name':'Synthetic QR Owner','bank_code':'sber','value':'','simultaneous_limit':'10'}
            response=await c.post('/trader/cabinet/requisites/create',data=fields,files={'qr_image':('qr.png',image_png(PAYLOAD),'image/png')})
            assert response.status_code==303,response.text
            async with AsyncSession(engine) as db:
                req=await db.scalar(select(m.Requisite).where(m.Requisite.full_name=='Synthetic QR Owner'));assert req and req.qr_identity
                rid=req.id
            invalid={**fields,'csrf_token':csrf(await c.get('/trader/cabinet/tradespace/requisites')),'full_name':'Invalid QR'}
            bad=await c.post('/trader/cabinet/requisites/create',data=invalid,files={'qr_image':('bad.png',b'invalid','image/png')});assert bad.status_code==303
            assert 'ui_error=qr_image_invalid' in bad.headers['location']
            rendered=await c.get(bad.headers['location'],follow_redirects=True)
            assert 'Не удалось прочитать PNG/JPEG с QR-кодом.' in rendered.text
            async with AsyncSession(engine) as db:assert await db.scalar(select(m.Requisite.id).where(m.Requisite.full_name=='Invalid QR')) is None
            oid,_=result(await create(c,'merchant',keys,(None,None)),'merchant')
            page=await c.get('/trader/cabinet/tradespace/operations/'+str(oid));assert page.status_code==200 and 'Не оплачено' not in page.text and 'Выданный QR NSPK' in page.text
            url='/trader/cabinet/deposits/'+str(oid)+'/'+decision
            denied=await c.post(url,data={'reason':'Synthetic'});assert denied.status_code==403
            good=await c.post(url,data={'csrf_token':csrf(page),'reason':'Synthetic no receipt'})
            assert good.status_code==(303 if decision=='confirm' else 403),good.text
            async with AsyncSession(engine) as db:
                dep=await db.get(m.Deposit,oid);assert dep.status==('paid' if decision=='confirm' else 'pending')
                assert dep.qr_reserved == (decision=='decline')
            # Merchant cannot mutate a trader requisite through a direct URL.
            async with client() as foreign:
                await login(foreign,users['merchant'],realm='merchant')
                form=await foreign.get('/merchant/cabinet/tradespace/overview')
                blocked=await foreign.post('/merchant/cabinet/requisites/'+str(rid)+'/edit',data={**fields,'csrf_token':csrf(form),'full_name':'Stolen'})
                assert blocked.status_code in {303,403}
            async with AsyncSession(engine) as db:assert (await db.get(m.Requisite,rid)).full_name=='Synthetic QR Owner'
    asyncio.run(run())

@pytest.mark.parametrize('origin',['merchant','aggregator'])
def test_qr_retains_rolling_and_referral_financial_snapshots(monkeypatch,origin):
    from tests.test_final_acceptance_combined_postgres import fixture,snapshot,assert_paid,reconcile
    from tests.test_rolling_postgres import _strict_quote
    from app.services import rolling
    from types import SimpleNamespace
    async def fixed():return _strict_quote(datetime.now(timezone.utc),'100')
    monkeypatch.setattr(rolling,'get_strict_rolling_ask_quote',fixed)
    async def run():
        async with isolated(monkeypatch) as (engine,_merchant,users,_keys),client() as c:
            ids=await fixture(engine,users['superadmin'])
            async with AsyncSession(engine,expire_on_commit=False) as db:
                merchant=await db.get(m.Merchant,ids['merchant'])
            if origin=='aggregator':
                account=await seed(engine,merchant);agg=await issue(engine,account,users['superadmin'],'sandbox')
            else:agg=(None,None)
            async with AsyncSession(engine) as db:
                req=await db.scalar(select(m.Requisite).where(m.Requisite.trader_id==ids['trader']))
                req.method='sbp_qr';req.qr_identity=validate_payload(PAYLOAD).identity;req.value_encrypted=encrypt_secret(PAYLOAD)
                for rule in await db.scalars(select(m.FeeRule).where(m.FeeRule.entity_id.in_([ids['trader'],ids['merchant'],account.id if origin=='aggregator' else ids['merchant']]))):rule.method=rule.payment_method='sbp_qr'
                await db.commit()
            oid,_=result(await create(c,origin,{'sandbox':(ids['key'],ids['secret'])},agg),origin)
            async with AsyncSession(engine) as db:
                for rule in await db.scalars(select(m.FeeRule).where(m.FeeRule.payment_method=='sbp_qr')):rule.rate_percent=40
                await db.commit()
            await confirm(engine,oid,SimpleNamespace(id=ids['trader']))
            after=await snapshot(engine,ids,oid)
            if origin=='merchant':assert_paid(after)
            else:
                assert after['trader']==(D('99000'),D('0'))
                assert after['merchant']==(D('1300'),D('0'))
                assert after['rolling']==(D('6'),D('6'),D('0'),'exhausted')
                assert after['fees']==(D('100'),D('50'),D('50'),'settled')
                assert after['teamlead']==(D('5'),D('0'),D('0'),D('5'))
            await reconcile(engine,ids)
    asyncio.run(run())
