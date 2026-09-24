"""Real PostgreSQL schemas, synthetic actors, no external delivery or real money.

Production ENV is simulated only inside this test process. Current dev config,
its immutable Sandbox binding and its merchants are never activated.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
import uuid

import httpx
import pyotp
import pytest
from redis.asyncio import Redis
from sqlalchemy import select, text, func
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app import models as m
from app.core.config import settings
from app.core.security import encrypt_secret, hash_password
from app.db.base import Base
from app.db.session import get_db
from app.main import app
import app.api.deps as deps
from app.services.integration_modes import (
    IntegrationModeError, verify_environment, integration_status, change_production_access,
)
from app.services.merchant_api_keys import issue_merchant_api_key
from app.services.webhook_signing_keys import issue_webhook_signing_key
from tests.test_merchant_hmac_v2_postgres import _signed_headers, _payload
from tests.tradespace.test_wave4_teamlead import csrf, login, PASSWORD, OTP

MANAGEMENT = '/api/v1/integration/merchants/'
COMMAND = {'confirmation': 'PRODUCTION', 'reason': 'Synthetic integration acceptance'}


@asynccontextmanager
async def isolated(monkeypatch, mode='sandbox'):
    url = os.environ['TEST_DATABASE_URL']
    assert url.endswith('/tradespace_test') and os.environ['ENV'] == 'test'
    schema = 'integration_test_' + uuid.uuid4().hex
    control = create_async_engine(url)
    async with control.begin() as c:
        await c.execute(text('CREATE SCHEMA ' + schema))
    engine = create_async_engine(url, connect_args={'server_settings': {'search_path': schema}})
    old_override = app.dependency_overrides.get(get_db)
    replay = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    with monkeypatch.context() as patch:
        patch.setattr(settings, 'ENV', 'production' if mode == 'production' else 'test')
        patch.setattr(settings, 'PRODUCTION_ACTIVATION_ENABLED', True)
        patch.setattr(deps, '_merchant_replay_redis', replay)
        import app.services.deposit_confirmation as confirmation_service
        patch.setattr(confirmation_service, 'enqueue_webhook_delivery', lambda *_: True)
        async def db_override():
            async with AsyncSession(engine, expire_on_commit=False) as db:
                await verify_environment(db)
                yield db
        app.dependency_overrides[get_db] = db_override
        try:
            async with engine.begin() as c:
                await c.run_sync(Base.metadata.create_all)
            async with AsyncSession(engine, expire_on_commit=False) as db:
                db.add(m.IntegrationEnvironment(id=1, mode=mode))
                users = {}
                for role in ('superadmin','admin','support','teamlead','merchant','trader','aggregator'):
                    user = m.User(email=f'{role}-{uuid.uuid4().hex}@example.test', role=role,
                        password_hash=hash_password(PASSWORD), twofa_enabled=role in {'superadmin','admin','support','teamlead'},
                        twofa_secret=encrypt_secret(OTP), trader_balance=Decimal('100000'))
                    db.add(user); users[role] = user
                await db.flush()
                merchant = m.Merchant(owner_id=users['merchant'].id, name='Isolated synthetic merchant',
                    webhook_url='https://8.8.8.8/callback', sandbox_mode=True)
                db.add(merchant); await db.flush()
                db.add(m.Balance(merchant_id=merchant.id, currency='RUB', available=Decimal('0'), frozen=Decimal('0')))
                keys = {}
                for key_mode in ('sandbox','production'):
                    key, secret = await issue_merchant_api_key(db, merchant, key_mode)
                    keys[key_mode] = (key.api_key, secret)
                await issue_webhook_signing_key(db, merchant, created_by=users['superadmin'].id)
                for kind, identifier, side, rate in [('merchant',merchant.id,'merchant_fee','10'),('trader',users['trader'].id,'executor_fee','5')]:
                    db.add(m.FeeRule(entity_type=kind,entity_id=identifier,fee_side=side,method='sbp',payment_method='sbp',currency='RUB',
                        min_amount=Decimal('0'),percent=Decimal(rate),rate_percent=Decimal(rate),effective_from=datetime.now(timezone.utc)-timedelta(days=1)))
                db.add(m.Requisite(trader_id=users['trader'].id,owner_name='Synthetic',full_name='Synthetic',method='sbp',
                    bank_code='sberbank',bank_name='СберБанк',value_encrypted=encrypt_secret('+79990001122'),enabled=True,status='active',
                    min_check=Decimal('1'),max_check=Decimal('100000'),simultaneous_limit=10,daily_limit=Decimal('1000000'),request_count=100,operation_limit=100))
                await db.commit()
            yield engine, merchant, users, keys
        finally:
            if old_override is None: app.dependency_overrides.pop(get_db, None)
            else: app.dependency_overrides[get_db] = old_override
            await replay.aclose()
            await engine.dispose()
            async with control.begin() as c:
                await c.execute(text('DROP SCHEMA ' + schema + ' CASCADE'))
            await control.dispose()


def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://localhost')


async def token_login(c, user):
    result = await c.post('/api/v1/auth/login',json={'email':user.email,'password':PASSWORD,'otp':pyotp.TOTP(OTP).now()})
    assert result.status_code == 200, result.text
    c.headers['Authorization'] = 'Bearer ' + result.json()['access_token']


async def activate(db, merchant, actor):
    return await change_production_access(db,merchant.id,actor,action='activate',**COMMAND)


async def money(db, merchant):
    balance = await db.scalar(select(m.Balance).where(m.Balance.merchant_id == merchant.id))
    return (balance.available,balance.frozen,await db.scalar(select(func.count()).select_from(m.Deposit)),
            await db.scalar(select(func.count()).select_from(m.LedgerEntry)))


@pytest.mark.parametrize('role', ['merchant','admin','support','teamlead','trader','aggregator'])
def test_only_superadmin_can_activate_http(monkeypatch, role):
    async def run():
        async with isolated(monkeypatch,'production') as (engine, merchant, users, keys), client() as c:
            await token_login(c,users[role])
            response = await c.post(MANAGEMENT+str(merchant.id)+'/production/activate',json=COMMAND)
            assert response.status_code == 403, response.text
            async with AsyncSession(engine) as db:
                assert await db.get(m.MerchantProductionAccess,merchant.id) is None
    asyncio.run(run())


@pytest.mark.parametrize('condition,code', [
    ('owner_flag','owner_activation_disabled'), ('webhook','https_webhook_required'),
    ('signing','webhook_key_required'), ('credentials','production_key_required'),
    ('fee','merchant_fee_required'), ('locked','merchant_unavailable'),
])
def test_activation_preconditions_are_server_side(monkeypatch,condition,code):
    async def run():
        async with isolated(monkeypatch,'production') as (engine, merchant, users, keys):
            async with AsyncSession(engine,expire_on_commit=False) as db:
                row = await db.get(m.Merchant,merchant.id)
                if condition == 'owner_flag': monkeypatch.setattr(settings,'PRODUCTION_ACTIVATION_ENABLED',False)
                elif condition == 'webhook': row.webhook_url = ''
                elif condition == 'locked': (await db.get(m.User,row.owner_id)).is_locked = True
                elif condition == 'signing': (await db.scalar(select(m.MerchantWebhookSigningKey))).status = 'revoked'
                elif condition == 'credentials': (await db.scalar(select(m.ApiKey).where(m.ApiKey.mode=='production'))).is_active = False
                elif condition == 'fee': (await db.scalar(select(m.FeeRule).where(m.FeeRule.entity_type=='merchant'))).is_active = False
                await db.flush(); before = await money(db,merchant)
                with pytest.raises(IntegrationModeError) as err: await activate(db,row,users['superadmin'])
                assert code in [r['code'] for r in err.value.reasons]
                assert await money(db,merchant) == before
                assert await db.get(m.MerchantProductionAccess,merchant.id) is None
    asyncio.run(run())


def test_activation_confirmation_idempotency_concurrency_audit_and_suspension(monkeypatch):
    async def run():
        async with isolated(monkeypatch,'production') as (engine, merchant, users, keys), client() as c:
            await token_login(c,users['superadmin']);path=MANAGEMENT+str(merchant.id)+'/production/'
            for confirmation in ('','sandbox'):
                response=await c.post(path+'activate',json={**COMMAND,'confirmation':confirmation})
                assert response.status_code==422
            async with AsyncSession(engine) as db: before=await money(db,merchant)
            results=await asyncio.wait_for(asyncio.gather(*(c.post(path+'activate',json=COMMAND) for _ in range(2))),15)
            assert [r.status_code for r in results]==[200,200],[r.text for r in results]
            async with AsyncSession(engine) as db:
                events=(await db.scalars(select(m.AuditLog).where(m.AuditLog.action=='merchant_production_activate'))).all()
                assert len(events)==1 and events[0].details['before']=='not_activated' and events[0].details['after']=='active'
                assert events[0].actor_id==users['superadmin'].id and events[0].details['reason']==COMMAND['reason']
                assert await money(db,merchant)==before
            result=await c.post(path+'suspend',json={**COMMAND,'confirmation':'SUSPEND'})
            assert result.status_code==200 and result.json()['status']=='suspended'
            key,secret=keys['production'];balance_path='/api/v1/merchant/balance'
            headers=_signed_headers(api_key=key,secret=secret,body=b'',nonce=uuid.uuid4().hex,idempotency_key=None,method='GET',signed_path=balance_path)
            denied=await c.get(balance_path,headers=headers)
            assert denied.status_code==403 and denied.json()['error']['code']=='production_not_active'
            async with AsyncSession(engine) as db: assert await money(db,merchant)==before
    asyncio.run(run())


@pytest.mark.parametrize('environment', ['sandbox','production'])
def test_hmac_keys_are_separate_and_cross_mode_requests_cannot_create_orders(monkeypatch,environment):
    async def run():
        async with isolated(monkeypatch,environment) as (engine, merchant, users, keys), client() as c:
            assert keys['sandbox'][0].startswith('pk_test_') and keys['production'][0].startswith('pk_live_')
            assert keys['sandbox'][1]!=keys['production'][1]
            if environment=='production':
                # A correctly signed key alone is insufficient.
                path='/api/v1/merchant/balance';key,secret=keys['production']
                headers=_signed_headers(api_key=key,secret=secret,body=b'',nonce=uuid.uuid4().hex,idempotency_key=None,method='GET',signed_path=path)
                blocked=await c.get(path,headers=headers)
                assert blocked.status_code==403 and blocked.json()['error']['code']=='production_not_active'
                async with AsyncSession(engine,expire_on_commit=False) as db:
                    await activate(db,merchant,users['superadmin']);await db.commit()
            other='production' if environment=='sandbox' else 'sandbox'
            path='/api/v1/merchant/deposits';body=_payload(uuid.uuid4().hex,amount='1000')
            key,secret=keys[other]
            headers=_signed_headers(api_key=key,secret=secret,body=body,nonce=uuid.uuid4().hex,idempotency_key=uuid.uuid4().hex,signed_path=path)
            denied=await c.post(path,headers=headers,content=body)
            assert denied.status_code==403 and denied.json()['error']['code']=='api_key_environment_mismatch'
            async with AsyncSession(engine) as db: assert await money(db,merchant)==(Decimal('0'),Decimal('0'),0,0)
            key,secret=keys[environment]
            headers=_signed_headers(api_key=key,secret=secret,body=body,nonce=uuid.uuid4().hex,idempotency_key=uuid.uuid4().hex,signed_path=path)
            response=await c.post(path,headers=headers,content=body)
            assert response.status_code==200,response.text
            async with AsyncSession(engine) as db:
                deposit=await db.scalar(select(m.Deposit))
                assert deposit.metadata_json['integration_mode']==environment and deposit.status=='pending'
                trader=await db.get(m.User,users['trader'].id)
                assert trader.trader_hold==Decimal('950') and trader.trader_balance==Decimal('100000')
                assert (await money(db,merchant))[:2]==(Decimal('0'),Decimal('0'))
            # Same money outcome in either independent environment; no callback HTTP is sent.
            from app.services.deposit_confirmation import confirm_deposit_payment, DepositConfirmationConflict
            async with AsyncSession(engine,expire_on_commit=False) as db:
                result=await confirm_deposit_payment(db,deposit.id,actor_id=users['trader'].id,
                    actor_ip='127.0.0.1',audit_action='integration_test_confirm',description='Synthetic confirmation')
                assert result.confirmed and result.status=='paid'
                trader=await db.get(m.User,users['trader'].id)
                assert trader.trader_hold==0 and trader.trader_balance==Decimal('99050')
                assert (await money(db,merchant))[:2]==(Decimal('900'),Decimal('0'))
                snapshot=await db.scalar(select(m.OperationFeeSnapshot))
                assert snapshot.merchant_fee_amount==100 and snapshot.executor_fee_amount==50 and snapshot.platform_income_amount==50
                with pytest.raises(DepositConfirmationConflict):
                    await confirm_deposit_payment(db,deposit.id,actor_id=users['trader'].id,
                        actor_ip='127.0.0.1',audit_action='integration_test_confirm',description='Synthetic duplicate')
            async with AsyncSession(engine) as db:
                assert await db.scalar(select(func.count()).select_from(m.WebhookEvent))==1
                assert (await money(db,merchant))[:2]==(Decimal('900'),Decimal('0'))
    asyncio.run(run())


def test_sandbox_cannot_be_promoted_and_status_does_not_leak_other_merchants(monkeypatch):
    async def run():
        async with isolated(monkeypatch) as (engine,merchant,users,keys),client() as c:
            await token_login(c,users['merchant'])
            response=await c.get(MANAGEMENT+str(merchant.id));assert response.status_code==200
            assert response.json()['environment']=='sandbox'
            assert 'production_environment_required' in [r['code'] for r in response.json()['blocking_reasons']]
            assert 'sk_' not in response.text and keys['production'][0] not in response.text
            assert (await c.get(MANAGEMENT+str(uuid.uuid4()))).status_code==404
            await token_login(c,users['superadmin'])
            assert (await c.post(MANAGEMENT+str(merchant.id)+'/production/activate',json=COMMAND)).status_code==409
            async with AsyncSession(engine) as db:
                assert (await db.get(m.Merchant,merchant.id)).sandbox_mode is True
                assert await db.get(m.MerchantProductionAccess,merchant.id) is None
                monkeypatch.setattr(settings,'ENV','production')
                with pytest.raises(IntegrationModeError,match='Режим сервера'): await verify_environment(db)
    asyncio.run(run())


def test_browser_ui_csrf_2fa_and_old_checkbox_cannot_bypass_activation(monkeypatch):
    async def run():
        async with isolated(monkeypatch) as (engine,merchant,users,keys),client() as c:
            await login(c,users['superadmin'])
            url='/staff/cabinet/tradespace/integrations?tab=credentials&id='+str(merchant.id)
            page=await c.get(url);assert page.status_code==200,page.text
            assert 'Подключить Production' in page.text and 'disabled title="Сначала выполните условия' in page.text
            path=f'/staff/cabinet/merchants/{merchant.id}/production/activate'
            assert (await c.post(path,data=COMMAND)).status_code==403
            response=await c.post(path,data={**COMMAND,'csrf_token':csrf(page)})
            assert response.status_code==303
            checkbox=await c.post(f'/staff/cabinet/merchants/{merchant.id}/integration',data={'csrf_token':csrf(page),'sandbox_mode':'off','webhook_url':'https://8.8.8.8/callback'})
            assert checkbox.status_code==303
            async with AsyncSession(engine) as db: assert (await db.get(m.Merchant,merchant.id)).sandbox_mode is True
        async with isolated(monkeypatch,'production') as (engine,merchant,users,keys),client() as c:
            await login(c,users['superadmin'])
            page=await c.get('/staff/cabinet/tradespace/integrations?tab=credentials&id='+str(merchant.id))
            assert page.status_code==200 and 'disabled title="Сначала выполните условия' not in page.text
            response=await c.post(f'/staff/cabinet/merchants/{merchant.id}/production/activate',data={**COMMAND,'csrf_token':csrf(page)})
            assert response.status_code==303
            async with AsyncSession(engine) as db: assert (await db.get(m.MerchantProductionAccess,merchant.id)).status=='active'
    asyncio.run(run())


@pytest.mark.parametrize('populated', [False, True])
def test_real_migration_production_clean_only_and_binding_immutable(monkeypatch,populated):
    from alembic.config import Config
    from alembic import command
    import psycopg
    from psycopg import sql
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from tests.test_migrations_postgres import _database_urls
    admin_url, url, name = _database_urls()
    with psycopg.connect(admin_url,autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    try:
        monkeypatch.setattr(settings,'SYNC_DATABASE_URL',url.replace('postgresql://','postgresql+psycopg://',1))
        monkeypatch.setattr(settings,'ENV','test')
        cfg=Config('alembic.ini')
        # Cache ini options, then keep env.py from reconfiguring pytest's global
        # logging handlers. The real migration still runs unchanged.
        assert cfg.get_main_option('script_location')
        cfg.config_file_name=None
        command.upgrade(cfg,'0025_teamlead_merchant_referrals')
        if populated:
            engine=create_engine(settings.SYNC_DATABASE_URL)
            with Session(engine) as db:
                db.add(m.User(email='synthetic@example.test',password_hash=hash_password(PASSWORD),role='merchant'));db.commit()
            engine.dispose()
        monkeypatch.setattr(settings,'ENV','production')
        if populated:
            with pytest.raises(RuntimeError,match='fresh empty database'):command.upgrade(cfg,'head')
            with psycopg.connect(url) as db:
                assert db.execute('SELECT count(*) FROM users').fetchone()[0]==1
                assert db.execute('SELECT version_num FROM alembic_version').fetchone()[0]=='0025_teamlead_merchant_referrals'
        else:
            command.upgrade(cfg,'head')
            with psycopg.connect(url) as db:
                assert db.execute('SELECT mode FROM integration_environment').fetchone()[0]=='production'
                assert db.execute('SELECT count(*) FROM merchant_production_access').fetchone()[0]==0
                for statement in ("UPDATE integration_environment SET mode='sandbox'", 'DELETE FROM integration_environment', 'TRUNCATE integration_environment'):
                    with pytest.raises(psycopg.errors.RaiseException,match='immutable'):
                        with db.transaction():db.execute(statement)
            with pytest.raises(RuntimeError,match='Production database boundary'):command.downgrade(cfg,'0025_teamlead_merchant_referrals')
    finally:
        assert name.startswith('tradespace_migration_')
        with psycopg.connect(admin_url,autocommit=True) as admin:
            admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


def test_production_management_requires_2fa_and_key_rotation_preserves_other_mode(monkeypatch):
    from app.services.merchant_api_keys import rotate_merchant_api_key
    async def run():
        async with isolated(monkeypatch,'production') as (engine,merchant,users,keys):
            async with AsyncSession(engine,expire_on_commit=False) as db:
                actor=await db.get(m.User,users['superadmin'].id);actor.twofa_enabled=False;await db.commit()
                with pytest.raises(IntegrationModeError) as err:await activate(db,merchant,actor)
                assert err.value.status==403
                old=await db.scalar(select(m.ApiKey).where(m.ApiKey.mode=='production'))
                new,secret=await rotate_merchant_api_key(db,merchant,old)
                await db.commit()
                sandbox=await db.scalar(select(m.ApiKey).where(m.ApiKey.mode=='sandbox'))
                assert sandbox.is_active and sandbox.api_key==keys['sandbox'][0]
                assert not old.is_active and new.is_active and new.api_key!=keys['production'][0] and secret!=keys['production'][1]
                assert await db.get(m.MerchantProductionAccess,merchant.id) is None
    asyncio.run(run())
