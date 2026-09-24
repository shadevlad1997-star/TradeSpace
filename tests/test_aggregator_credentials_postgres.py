"""Synthetic independent PostgreSQL environments; no real Production or payments."""
import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app import models as m
from app.core.config import settings
from app.core.security import encrypt_secret, decrypt_secret, sign_hmac
from app.services.aggregator_credentials import change_aggregator_key, change_aggregator_access
from app.services.integration_modes import IntegrationModeError
from tests.test_integration_modes_postgres import isolated, client, token_login, COMMAND
from tests.tradespace.test_wave4_teamlead import login, csrf

API='/api/v1/aggregator/v1/payments'
MANAGEMENT='/api/v1/integration/aggregators/'

async def seed(engine, merchant):
    async with AsyncSession(engine,expire_on_commit=False) as db:
        account=m.AggregatorAccount(platform_merchant_id=merchant.id,name='Synthetic '+uuid.uuid4().hex,
            api_key='unissued_'+uuid.uuid4().hex,secret_hash='',status='active',callback_url='https://8.8.8.8/callback')
        db.add(account);await db.flush()
        db.add(m.FeeRule(entity_type='aggregator',entity_id=account.id,fee_side='executor_fee',method='sbp',payment_method='sbp',currency='RUB',
            min_amount=Decimal('0'),percent=Decimal('5'),rate_percent=Decimal('5'),effective_from=datetime.now(timezone.utc)-timedelta(days=1)))
        await db.commit();return account

async def issue(engine, account, root, mode):
    async with AsyncSession(engine,expire_on_commit=False) as db:
        key,secret=await change_aggregator_key(db,account.id,root,action='issue',mode=mode,confirmation=mode.upper(),reason=COMMAND['reason'])
        await db.commit();return key,secret

async def activate(engine,account,root):
    async with AsyncSession(engine) as db:
        await change_aggregator_access(db,account.id,root,action='activate',**COMMAND);await db.commit()

async def unchanged_state(engine):
    async with AsyncSession(engine) as db:
        counts=[await db.scalar(select(func.count()).select_from(model)) for model in
            (m.Deposit,m.AggregatorPayment,m.AggregatorReplayNonce,m.LedgerEntry,m.TraderLedgerEntry,m.OperationFeeSnapshot,m.WebhookEvent,m.AggregatorCallbackLog)]
        balances=list((await db.execute(select(m.Balance.available,m.Balance.frozen).order_by(m.Balance.id))).all())
        traders=list((await db.execute(select(m.User.trader_balance,m.User.trader_hold).order_by(m.User.id))).all())
        return counts,balances,traders

async def payment(c,key,secret,tag=None):
    tag=tag or uuid.uuid4().hex
    body=json.dumps(dict(aggregator_order_id=tag,merchant_order_id=tag,external_merchant_id='synthetic',amount='1000',currency='RUB',payment_method='sbp'),separators=(',',':')).encode()
    ts=str(int(time.time()))
    return await c.post(API,content=body,headers={'Content-Type':'application/json','X-API-Key':key.api_key,
        'X-Timestamp':ts,'X-Signature':sign_hmac(secret,ts,body),'X-Request-ID':tag,'X-Idempotency-Key':tag})

@pytest.mark.parametrize('mode',['sandbox','production'])
def test_signed_api_financial_compatibility_replay_and_separate_admission(monkeypatch,mode):
    async def run():
        async with isolated(monkeypatch,mode) as (engine,merchant,users,_),client() as c:
            account=await seed(engine,merchant);key,secret=await issue(engine,account,users['superadmin'],mode)
            assert key.api_key.startswith('ak_live_' if mode=='production' else 'ak_test_')
            if mode=='production':
                before=await unchanged_state(engine)
                denied=await payment(c,key,secret);assert denied.status_code==403 and 'production_not_active' in denied.text
                assert await unchanged_state(engine)==before
                await activate(engine,account,users['superadmin'])
            # Aggregator admission must not depend on MerchantProductionAccess.
            async with AsyncSession(engine) as db: assert await db.get(m.MerchantProductionAccess,merchant.id) is None
            tag=uuid.uuid4().hex;r=await payment(c,key,secret,tag);assert r.status_code==200,r.text
            dep_id=uuid.UUID(r.json()['platform_payment_id'])
            async with AsyncSession(engine) as db:
                trader=await db.get(m.User,users['trader'].id)
                # Aggregator executor retains the existing 100% trader hold (zero trader fee).
                assert trader.trader_hold==Decimal('1000')
                snap=await db.scalar(select(m.OperationFeeSnapshot));assert snap.merchant_fee_amount==100 and snap.executor_fee_amount==50 and snap.platform_income_amount==50
            before=await unchanged_state(engine)
            duplicate=await payment(c,key,secret,tag);assert duplicate.status_code==409 and 'hmac replay detected' in duplicate.text
            assert await unchanged_state(engine)==before
            # A retry after nonce expiry is the SAME operation, not another debit.
            async with AsyncSession(engine) as db:
                # Emulate expiration cleanup before an admissible transport retry.
                for nonce in (await db.scalars(select(m.AggregatorReplayNonce))).all(): await db.delete(nonce)
                await db.commit()
            retry=await payment(c,key,secret,tag);assert retry.status_code==200 and retry.json()['platform_payment_id']==str(dep_id)
            from app.services.deposit_confirmation import confirm_deposit_payment,DepositConfirmationConflict
            async with AsyncSession(engine,expire_on_commit=False) as db:
                result=await confirm_deposit_payment(db,dep_id,actor_id=users['trader'].id,actor_ip='127.0.0.1',audit_action='synthetic_aggregator_confirm',description='Synthetic acceptance')
                assert result.status=='paid'
                with pytest.raises(DepositConfirmationConflict):
                    await confirm_deposit_payment(db,dep_id,actor_id=users['trader'].id,actor_ip='127.0.0.1',audit_action='synthetic_aggregator_confirm',description='Duplicate')
                balance=await db.scalar(select(m.Balance).where(m.Balance.merchant_id==merchant.id));assert balance.available==900
                trader=await db.get(m.User,users['trader'].id);assert trader.trader_hold==0 and trader.trader_balance==99000
                assert await db.scalar(select(func.count()).select_from(m.Deposit))==1
    asyncio.run(run())

@pytest.mark.parametrize('mode',['sandbox','production'])
@pytest.mark.parametrize('condition',['wrong_environment','suspended_key','revoked_key','blocked_account','archived_account'])
def test_denials_precede_all_financial_and_replay_writes(monkeypatch,mode,condition):
    async def run():
        async with isolated(monkeypatch,mode) as (engine,merchant,users,_),client() as c:
            account=await seed(engine,merchant);key,secret=await issue(engine,account,users['superadmin'],mode)
            if mode=='production':await activate(engine,account,users['superadmin'])
            async with AsyncSession(engine) as db:
                record=await db.get(m.AggregatorApiKey,key.id);a=await db.get(m.AggregatorAccount,account.id)
                if condition=='wrong_environment':record.mode='production' if mode=='sandbox' else 'sandbox'
                elif condition=='suspended_key':record.status='suspended'
                elif condition=='revoked_key':record.status='revoked'
                elif condition=='blocked_account':a.status='blocked'
                else:a.is_archived=True
                await db.commit()
            before=await unchanged_state(engine);r=await payment(c,key,secret);assert r.status_code==403,r.text
            assert await unchanged_state(engine)==before
            async with AsyncSession(engine) as db:assert (await db.get(m.AggregatorApiKey,key.id)).last_used_at is None
    asyncio.run(run())

@pytest.mark.parametrize('role',['admin','support','merchant','trader','teamlead','aggregator'])
def test_management_superadmin_only(monkeypatch,role):
    async def run():
        async with isolated(monkeypatch,'production') as (engine,merchant,users,_),client() as c:
            account=await seed(engine,merchant);await token_login(c,users[role]);base=MANAGEMENT+str(account.id)
            for path,body in [('/credentials/issue',dict(mode='production',**COMMAND)),('/production/activate',COMMAND)]:
                r=await c.post(base+path,json=body);assert r.status_code==403
            async with AsyncSession(engine) as db:
                assert await db.scalar(select(func.count()).select_from(m.AggregatorApiKey))==0
                assert await db.get(m.AggregatorProductionAccess,account.id) is None
    asyncio.run(run())

@pytest.mark.parametrize('condition',['wrong_environment','owner_flag','missing_key','missing_fee','blocked','twofa','confirmation'])
def test_server_activation_and_issuance_preconditions(monkeypatch,condition):
    async def run():
        mode='sandbox' if condition=='wrong_environment' else 'production'
        async with isolated(monkeypatch,mode) as (engine,merchant,users,_),client() as c:
            account=await seed(engine,merchant)
            if condition not in {'missing_key','wrong_environment','owner_flag'}:await issue(engine,account,users['superadmin'],mode)
            await token_login(c,users['superadmin']);base=MANAGEMENT+str(account.id)
            if condition=='owner_flag':monkeypatch.setattr(settings,'PRODUCTION_ACTIVATION_ENABLED',False)
            async with AsyncSession(engine) as db:
                if condition=='missing_fee':
                    fee=await db.scalar(select(m.FeeRule).where(m.FeeRule.entity_type=='aggregator'));fee.is_active=False
                elif condition=='blocked':(await db.get(m.AggregatorAccount,account.id)).status='blocked'
                elif condition=='twofa':(await db.get(m.User,users['superadmin'].id)).twofa_enabled=False
                await db.commit()
            if condition in {'wrong_environment','owner_flag'}:
                r=await c.post(base+'/credentials/issue',json=dict(mode='production',**COMMAND));assert r.status_code==403
            command={**COMMAND,'confirmation':''} if condition=='confirmation' else COMMAND
            r=await c.post(base+'/production/activate',json=command);assert r.status_code in {403,409,422},r.text
            async with AsyncSession(engine) as db:assert await db.get(m.AggregatorProductionAccess,account.id) is None
            assert (await unchanged_state(engine))[0]==[0]*8
    asyncio.run(run())

@pytest.mark.parametrize('mode',['sandbox','production'])
def test_one_time_issue_rotation_revoke_audit_and_concurrent_activation(monkeypatch,mode):
    async def run():
        async with isolated(monkeypatch,mode) as (engine,merchant,users,_),client() as c:
            account=await seed(engine,merchant);await token_login(c,users['superadmin']);base=MANAGEMENT+str(account.id)
            body=dict(mode=mode,reason=COMMAND['reason'],confirmation=mode.upper())
            results=await asyncio.gather(*(c.post(base+'/credentials/issue',json=body) for _ in range(2)))
            assert sorted(r.status_code for r in results)==[200,409]
            response=next(r for r in results if r.status_code==200);original=response.json()
            assert 'no-store' in response.headers['cache-control']
            assert 'secret_key' not in (await c.get(base)).text and original['api_key'] not in (await c.get(base)).text
            if mode=='production':
                results=await asyncio.gather(*(c.post(base+'/production/activate',json=COMMAND) for _ in range(2)))
                assert [r.status_code for r in results]==[200,200]
            rotated=await c.post(base+'/credentials/rotate',json={**body,'key_id':original['id']});assert rotated.status_code==200
            newer=rotated.json();assert newer['secret_key']!=original['secret_key'] and newer['api_key']!=original['api_key']
            assert (await c.post(base+'/credentials/rotate',json={**body,'key_id':original['id']})).status_code==409
            async with AsyncSession(engine) as db:
                old=await db.get(m.AggregatorApiKey,uuid.UUID(original['id']));assert old.status=='revoked'
                account_row=await db.get(m.AggregatorAccount,account.id);assert decrypt_secret(account_row.secret_hash)==newer['secret_key']
                logs=(await db.scalars(select(m.AuditLog))).all();dump=json.dumps([x.details for x in logs])
                assert all(value not in dump for value in [original['api_key'],original['secret_key'],newer['api_key'],newer['secret_key']])
                if mode=='production':assert sum(x.action=='aggregator_production_activate' for x in logs)==1
            # Account compatibility fields cannot resurrect the old key.
            assert (await payment(c,old,original['secret_key'])).status_code==403
            for action in ('suspend','revoke','revoke'):
                r=await c.post(base+'/credentials/'+action,json={**body,'key_id':newer['id'],'confirmation':action.upper()});assert r.status_code==200
                async with AsyncSession(engine) as db:key=await db.get(m.AggregatorApiKey,uuid.UUID(newer['id']))
                before=await unchanged_state(engine);assert (await payment(c,key,newer['secret_key'])).status_code==403
                assert await unchanged_state(engine)==before
    asyncio.run(run())


def test_access_suspension_owner_flag_and_no_merchant_coupling(monkeypatch):
    async def run():
        async with isolated(monkeypatch,'production') as (engine,merchant,users,_),client() as c:
            account=await seed(engine,merchant);key,secret=await issue(engine,account,users['superadmin'],'production');await activate(engine,account,users['superadmin'])
            await token_login(c,users['superadmin']);base=MANAGEMENT+str(account.id)
            monkeypatch.setattr(settings,'PRODUCTION_ACTIVATION_ENABLED',False)
            r=await c.post(base+'/production/suspend',json={**COMMAND,'confirmation':'SUSPEND'});assert r.status_code==200
            before=await unchanged_state(engine);r=await payment(c,key,secret);assert r.status_code==403 and 'production_not_active' in r.text
            assert await unchanged_state(engine)==before
            assert (await c.post(base+'/production/activate',json=COMMAND)).status_code==409
            assert (await c.post(base+'/credentials/rotate',json=dict(mode='production',key_id=str(key.id),**COMMAND))).status_code==403
            async with AsyncSession(engine) as db:assert await db.get(m.MerchantProductionAccess,merchant.id) is None
    asyncio.run(run())

@pytest.mark.parametrize('mode',['sandbox','production'])
def test_account_bootstrap_never_issues_implicit_production_key(monkeypatch,mode):
    from app.services.aggregators import create_aggregator_account
    async def run():
        async with isolated(monkeypatch,mode) as (engine,merchant,users,_):
            async with AsyncSession(engine,expire_on_commit=False) as db:
                account,secret=await create_aggregator_account(db,name='Synthetic bootstrap')
                key=await db.scalar(select(m.AggregatorApiKey).where(m.AggregatorApiKey.aggregator_id==account.id))
                if mode=='sandbox':assert key.mode=='sandbox' and decrypt_secret(key.encrypted_secret)==secret
                else:assert key is None and secret=='' and account.api_key.startswith('unissued_')
                assert await db.get(m.AggregatorProductionAccess,account.id) is None
    asyncio.run(run())


def test_browser_csrf_one_time_redis_secret_and_compatibility_route(monkeypatch):
    from redis.asyncio import Redis
    from app.web import routes as web
    async def run():
        async with isolated(monkeypatch) as (engine,merchant,users,_),client() as c:
            redis=Redis.from_url(settings.REDIS_URL,decode_responses=True);monkeypatch.setattr(web,'_secret_flash_redis',redis)
            try:
                account=await seed(engine,merchant);await login(c,users['superadmin'])
                page=await c.get('/staff/cabinet/tradespace/network?tab=aggregators&id='+str(account.id));assert page.status_code==200
                assert 'Sandbox' in page.text and 'Подключить Production' in page.text and 'Это Sandbox' in page.text
                path=f'/staff/cabinet/aggregators/{account.id}/credentials/issue';body=dict(mode='sandbox',confirmation='SANDBOX',reason=COMMAND['reason'])
                assert (await c.post(path,data=body)).status_code==403
                r=await c.post(path,data={**body,'csrf_token':csrf(page)});assert r.status_code==303
                first=await c.get(r.headers['location']);second=await c.get(r.headers['location'])
                assert first.status_code==200 and 'no-store' in first.headers['cache-control'] and 'aggregator-secret' in first.text
                assert 'Секрет уже просмотрен' in second.text and 'aggregator-secret' not in second.text
                async with AsyncSession(engine) as db:
                    key=await db.scalar(select(m.AggregatorApiKey));secret=decrypt_secret(key.encrypted_secret)
                    assert secret in first.text and secret not in second.text
                    assert all(secret not in cookie.value and key.api_key not in cookie.value for cookie in c.cookies.jar)
                old=f'/staff/cabinet/aggregators/{account.id}/secret'
                assert (await c.post(old,data={'csrf_token':csrf(page)})).status_code==303
                async with AsyncSession(engine) as db:assert await db.scalar(select(func.count()).select_from(m.AggregatorApiKey))==1
            finally:await redis.aclose()
    asyncio.run(run())



def test_real_additive_migration_preserves_existing_sandbox_credentials(monkeypatch):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    import psycopg
    from psycopg import sql
    from tests.test_migrations_postgres import _database_urls
    admin_url,url,name=_database_urls()
    with psycopg.connect(admin_url,autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    engine=None
    try:
        monkeypatch.setattr(settings,'SYNC_DATABASE_URL',url.replace('postgresql://','postgresql+psycopg://',1))
        monkeypatch.setattr(settings,'ENV','test')
        cfg=Config('alembic.ini');assert cfg.get_main_option('script_location');cfg.config_file_name=None
        command.upgrade(cfg,'0026_integration_modes')
        engine=create_engine(settings.SYNC_DATABASE_URL)
        old_key='ak_'+uuid.uuid4().hex;secret='synthetic-migration-secret';cipher=encrypt_secret(secret)
        with Session(engine,expire_on_commit=False) as db:
            owner=m.User(email=uuid.uuid4().hex+'@example.test',password_hash='disabled',role='aggregator',is_active=False)
            db.add(owner);db.flush();merchant=m.Merchant(owner_id=owner.id,name='Synthetic old account');db.add(merchant);db.flush()
            account=m.AggregatorAccount(platform_merchant_id=merchant.id,name='Synthetic migration',api_key=old_key,secret_hash=cipher,status='active')
            db.add(account);db.commit();aid=account.id
        command.upgrade(cfg,'head')
        with Session(engine) as db:
            key=db.scalar(select(m.AggregatorApiKey));assert key.aggregator_id==aid
            assert key.api_key==old_key and key.encrypted_secret==cipher and key.mode=='sandbox' and key.status=='active'
            assert decrypt_secret(key.encrypted_secret)==secret
            assert db.scalar(select(func.count()).select_from(m.AggregatorProductionAccess))==0
            assert db.scalar(select(func.count()).select_from(m.Deposit))==0
            assert db.scalar(select(func.count()).select_from(m.MerchantProductionAccess))==0
        with pytest.raises(RuntimeError,match='Cannot remove issued aggregator credentials'):
            command.downgrade(cfg,'0026_integration_modes')
        # Actual old HMAC headers/signature, against the upgraded PostgreSQL DB.
        async def request_after_migration():
            from sqlalchemy.ext.asyncio import create_async_engine
            from app.db.session import get_db
            from app.main import app
            from app.services.integration_modes import verify_environment
            async_engine=create_async_engine(url.replace('postgresql://','postgresql+asyncpg://',1))
            previous=app.dependency_overrides.get(get_db)
            async def dependency():
                async with AsyncSession(async_engine,expire_on_commit=False) as db:
                    await verify_environment(db);yield db
            app.dependency_overrides[get_db]=dependency
            try:
                ts=str(int(time.time()))
                async with client() as c:
                    r=await c.get(API+'/'+str(uuid.uuid4()),headers={'X-API-Key':old_key,'X-Timestamp':ts,'X-Signature':sign_hmac(secret,ts,b'')})
                    assert r.status_code==404 and 'payment not found' in r.text
            finally:
                if previous is None:app.dependency_overrides.pop(get_db,None)
                else:app.dependency_overrides[get_db]=previous
                await async_engine.dispose()
        asyncio.run(request_after_migration())
    finally:
        if engine:engine.dispose()
        assert name.startswith('tradespace_migration_')
        with psycopg.connect(admin_url,autocommit=True) as admin:
            admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


def test_foreign_key_id_and_unverified_staff_session_cannot_rotate(monkeypatch):
    from app.services.aggregator_credentials import change_aggregator_key
    async def run():
        async with isolated(monkeypatch) as (engine,merchant,users,_),client() as c:
            a=await seed(engine,merchant);b=await seed(engine,merchant)
            key,_=await issue(engine,a,users['superadmin'],'sandbox')
            async with AsyncSession(engine) as db:
                with pytest.raises(IntegrationModeError) as error:
                    await change_aggregator_key(db,b.id,users['superadmin'],action='rotate',mode='sandbox',key_id=key.id,
                        confirmation='SANDBOX',reason=COMMAND['reason'])
                assert error.value.status==404
            # The real browser command must also reject a staff session which
            # lacks verified second factor; role hiding alone is insufficient.
            from app.web import routes as web
            from starlette.requests import Request
            async def fake_user(*_):return users['superadmin']
            monkeypatch.setattr(web,'get_current_web_user',fake_user)
            scope={'type':'http','method':'POST','path':'/staff/cabinet','headers':[], 'session':{'uid':str(users['superadmin'].id)},'query_string':b''}
            async with AsyncSession(engine) as db:
                response=await web.cabinet_aggregator_credentials(Request(scope),a.id,'revoke',mode='sandbox',confirmation='REVOKE',reason=COMMAND['reason'],key_id=key.id,db=db)
                assert response.status_code==303 and 'login' in response.headers['location']
                assert (await db.get(m.AggregatorApiKey,key.id)).status=='active'
    asyncio.run(run())
