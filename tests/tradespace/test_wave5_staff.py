"""Staff UI contract: real PostgreSQL, real realm sessions/guards; all data rolls back."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime,timedelta,timezone
from decimal import Decimal
import re
import uuid
import httpx
from sqlalchemy import event,select,func
from sqlalchemy.ext.asyncio import AsyncSession
from app import models as m
from app.core.security import encrypt_secret
from app.db.session import engine,get_db
from app.main import app
from app.presentation.tradespace.staff.access import VIEWS
from app.presentation.tradespace.staff.view_models import load_staff_page
from tests.tradespace.test_wave4_teamlead import seed as seed_leads,login,csrf,PASSWORD,OTP,WALLET

BASE='/staff/cabinet'
CANARY='NEVER-DISPLAY-STAFF-SECRET'

async def seed(db):
    data=await seed_leads(db);now=datetime.now(timezone.utc);tag=uuid.uuid4().hex[:10]
    merchant=data['merchant'];trader=data['trader'];actor=data['super']
    trader.trader_balance=Decimal('555555.55');trader.trader_hold=Decimal('950.00')
    balance=m.Balance(merchant_id=merchant.id,available=Decimal('76543.21'),frozen=Decimal('1234.56'),currency='RUB')
    req=m.Requisite(trader_id=trader.id,owner_name='Тестовый получатель',method='sbp',value_encrypted=encrypt_secret('TEST-REQUISITE'),bank_code='sber',bank_name='Сбербанк',enabled=True,status='active')
    db.add_all([balance,req]);await db.flush()
    pending=m.Deposit(merchant_id=merchant.id,external_id='STAFF-PENDING-'+tag,amount=Decimal('1000'),currency='RUB',method='sbp',requisites_id=req.id,status='pending',expires_at=now+timedelta(minutes=15),metadata_json={'trader_hold_amount':'950','trader_settlement_amount':'950','trader_profit_amount':'50','trader_hold_status':'active','secret':CANARY})
    expired=m.Deposit(merchant_id=merchant.id,external_id='STAFF-EXPIRED-'+tag,amount=Decimal('4000'),currency='RUB',method='c2c',status='pending',expires_at=now-timedelta(days=1))
    payout=m.Payout(merchant_id=merchant.id,external_id='STAFF-PAYOUT-'+tag,amount=Decimal('321.09'),currency='RUB',method='sbp',status='completed',destination='Тестовый получатель')
    db.add_all([pending,expired,payout]);await db.flush()
    appeal=m.Appeal(operation_type='deposit',operation_id=data['deposits'][0].id,status='opened',created_by=data['merchantowner'].id,metadata_json={'secret':CANARY,'deadline_at':(now+timedelta(minutes=30)).isoformat()})
    settlement=m.MerchantSettlement(merchant_id=merchant.id,amount_usdt=Decimal('10'),fee_usdt=Decimal('2'),rate_rub=Decimal('100'),amount_rub=Decimal('1000'),fee_rub=Decimal('200'),total_debit_rub=Decimal('1200'),trc20_address=WALLET,network='TRC20',idempotency_key='wave5-settle-'+tag,status='pending')
    aggregator=m.AggregatorAccount(platform_merchant_id=merchant.id,name='Тестовый агрегатор',api_key='ak-'+CANARY+tag,secret_hash=encrypt_secret(CANARY),status='active')
    key=m.ApiKey(merchant_id=merchant.id,api_key='pk-'+CANARY+tag,secret_hash=encrypt_secret(CANARY),mode='sandbox')
    signing=m.MerchantWebhookSigningKey(merchant_id=merchant.id,key_id='public-key-'+tag,encrypted_secret=encrypt_secret(CANARY),status='active')
    webhook=m.WebhookEvent(merchant_id=merchant.id,event_type='deposit.paid',status='retry',attempts=1,max_attempts=5,last_status_code=500,last_error='token='+CANARY,payload={'operation_id':str(pending.id),'status':'paid','amount':'1000.00','currency':'RUB','secret':CANARY},correlation_id='staff-local-'+tag)
    rolling=m.MerchantRollingTransfer(merchant_id=merchant.id,sequence_no=1,amount_usdt=Decimal('100'),recovered_usdt=Decimal('0'),remaining_usdt=Decimal('0'),network='TRC20',destination_address=WALLET,tx_hash='test-rolling-'+tag,status='pending_confirmation',source='registered',created_by=actor.id,sent_at=now,idempotency_key='wave5-roll-'+tag)
    db.add_all([appeal,settlement,aggregator,key,signing,webhook,rolling]);await db.flush()
    db.add(m.AppealMessage(appeal_id=appeal.id,author_id=actor.id,message='Поступление проверяется по подтверждению мерчанта.'))
    db.add(m.WebhookDeliveryAttempt(webhook_event_id=webhook.id,attempt_no=1,status='failed',status_code=500,error='secret='+CANARY,response_snippet=CANARY))
    db.add(m.RiskEvent(operation_type='deposit',operation_id=pending.id,score=20,decision='review',reason='Тестовая проверка',source='test',target_type='trader',target_id=trader.id,trader_id=trader.id,requisite_id=req.id))
    rule_args=dict(method='sbp',percent=Decimal('0'),fixed=Decimal('0'),payment_method='sbp',currency='RUB',min_amount=Decimal('100'),max_amount=Decimal('150000.01'),effective_from=now-timedelta(days=20),is_active=True,version=1)
    merchant_rule=m.FeeRule(**rule_args,merchant_id=merchant.id,entity_type='merchant',entity_id=merchant.id,fee_side='merchant_fee',rate_percent=Decimal('15'))
    trader_rule=m.FeeRule(**rule_args,entity_type='trader',entity_id=trader.id,fee_side='executor_fee',rate_percent=Decimal('7'))
    db.add_all([merchant_rule,trader_rule]);await db.flush()
    snapshot=m.OperationFeeSnapshot(deposit_id=data['deposits'][0].id,merchant_id=merchant.id,merchant_rate_rule_id=merchant_rule.id,merchant_rate_version=1,merchant_rate_percent=Decimal('15'),merchant_fee_amount=Decimal('15000'),executor_type='trader',executor_id=trader.id,executor_rate_rule_id=trader_rule.id,executor_rate_version=1,executor_rate_percent=Decimal('7'),executor_fee_amount=Decimal('7000'),platform_margin_percent=Decimal('8'),platform_income_amount=Decimal('8000'),calculation_base_amount=Decimal('100000'),currency='RUB',payment_method='sbp',rate_snapshot_at=now,settlement_status='settled',settled_at=now)
    db.add(snapshot)
    db.add(m.TraderAntiscamSettings(trader_id=trader.id))
    db.add(m.RequisiteAntiscamSettings(requisite_id=req.id))
    # Add pagination data but preserve a different method/sum for filtering assertions.
    for index in range(28):db.add(m.Deposit(merchant_id=merchant.id,external_id=f'STAFF-HISTORY-{tag}-{index:02}',amount=Decimal('2000')+index,currency='RUB',method='sbp',status='paid',expires_at=now,created_at=now-timedelta(hours=index+1)))
    await db.flush()
    return {**data,'pending':pending,'expired':expired,'payout':payout,'requisite':req,'balance':balance,'appeal':appeal,'settlement':settlement,'aggregator':aggregator,'key':key,'webhook':webhook,'rolling':rolling,'snapshot':snapshot}

@asynccontextmanager
async def fixture():
    await engine.dispose(close=False)
    async with engine.connect() as conn:
        transaction=await conn.begin()
        async with AsyncSession(bind=conn,expire_on_commit=False,join_transaction_mode='create_savepoint') as db:
            data=await seed(db);await db.commit()
        async def override():
            async with AsyncSession(bind=conn,expire_on_commit=False,join_transaction_mode='create_savepoint') as db:yield db
        old=app.dependency_overrides.get(get_db);app.dependency_overrides[get_db]=override
        try:yield data,conn
        finally:
            if old is None:app.dependency_overrides.pop(get_db,None)
            else:app.dependency_overrides[get_db]=old
            await transaction.rollback()

def client():return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://localhost',headers={'X-TradeSpace-Preview':'wave5'})

def test_staff_role_matrix_and_read_queries_never_write():
    async def scenario():
        async with fixture() as (data,conn):
            for role,key in [('superadmin','super'),('admin','admin'),('support','support')]:
                async with client() as c:
                    await login(c,data[key]);writes=[]
                    def observe(_a,_b,statement,*_):
                        if statement.lstrip().split(' ',1)[0].upper() in {'UPDATE','INSERT','DELETE'}:writes.append(statement)
                    event.listen(engine.sync_engine,'before_cursor_execute',observe)
                    try:
                        landing=await c.get(BASE);assert landing.status_code==200;assert 'Центр управления' in landing.text
                        for v in VIEWS:
                            page=await c.get(f'{BASE}/tradespace/{v.section}?tab={v.slug}')
                            assert page.status_code==(200 if role in v.roles else 403),(role,v.slug,page.text[:600])
                            assert CANARY not in page.text
                            assert OTP not in page.text
                            assert 'no-store' in page.headers['cache-control']
                        assert not writes,writes
                    finally:event.remove(engine.sync_engine,'before_cursor_execute',observe)
            assert await conn.scalar(select(m.Deposit.status).where(m.Deposit.id==data['expired'].id))=='pending'
    asyncio.run(scenario())

def test_staff_details_scope_secrets_and_money_sources():
    async def scenario():
        async with fixture() as (data,conn),client() as c:
            await login(c,data['super'])
            cases=[('operations','deposits','pending'),('operations','deposits',None),('operations','payouts','payout'),('operations','appeals','appeal'),('network','traders','trader'),('network','merchants','merchant'),('network','teamleads','lead'),('network','requisites','requisite'),('network','aggregators','aggregator'),('finance','settlements','settlement'),('finance','rolling','rolling'),('finance','merchants','balance'),('finance','traders','trader'),('integrations','webhooks','webhook'),('integrations','credentials','merchant')]
            for section,tab,key in cases:
                ident=data[key].id if key else data['deposits'][0].id
                response=await c.get(f'{BASE}/tradespace/{section}?tab={tab}&id={ident}')
                assert response.status_code==200,(section,tab,response.text[:600])
                assert CANARY not in response.text
                if not key:
                    assert '15 000.00' in response.text and '7 000.00' in response.text and '8 000.00' in response.text
                if key=='balance':assert '76 543.21' in response.text and '1 234.56' in response.text
                if section=='finance' and key=='trader':assert '555 555.55' in response.text and '950.00' in response.text
                if key=='webhook':assert '500' in response.text and 'Повторить доставку' in response.text
                if key=='settlement':assert '/reject' in response.text and 'временно недоступно' not in response.text
            assert (await c.get(f'{BASE}/tradespace/network?tab=traders&id={data["super"].id}')).status_code==404
            assert (await c.get(f'{BASE}/tradespace/control?tab=users&id={data["trader"].id}')).status_code==404
            assert (await c.get(f'{BASE}/tradespace/network?tab=teamleads&id={data["merchant"].id}')).status_code==404
            async with AsyncSession(bind=conn,join_transaction_mode='create_savepoint') as db:
                page=await load_staff_page(db,data['support'],cabinet_base=BASE,query_params={'tab':'deposits','id':str(data['deposits'][0].id)},section='operations')
                assert 'platform_income_amount' not in repr(page)
                assert 'password_hash' not in repr(page) and CANARY not in repr(page)
    asyncio.run(scenario())

def test_staff_filters_pagination_and_invalid_identifiers():
    async def scenario():
        async with fixture() as (data,conn),client() as c:
            await login(c,data['admin'])
            url=f'{BASE}/tradespace/operations?tab=deposits'
            page=await c.get(url+'&q=STAFF-HISTORY-&sort=oldest');assert page.status_code==200 and 'Далее' in page.text
            async with AsyncSession(bind=conn,join_transaction_mode='create_savepoint') as db:
                first=await load_staff_page(db,data['admin'],cabinet_base=BASE,query_params={'tab':'deposits','q':'STAFF-HISTORY-','sort':'oldest'},section='operations')
                second=await load_staff_page(db,data['admin'],cabinet_base=BASE,query_params={'tab':'deposits','q':'STAFF-HISTORY-','sort':'oldest','page':'2'},section='operations')
                assert len(first.rows)==25 and len(second.rows)==3
                assert not ({r['id'] for r in first.rows}&{r['id'] for r in second.rows})
                filtered=await load_staff_page(db,data['admin'],cabinet_base=BASE,query_params={'tab':'deposits','method':'c2c','merchant_id':str(data['merchant'].id),'amount_min':'3900','amount_max':'4100'},section='operations')
                assert [r['id'] for r in filtered.rows]==[str(data['expired'].id)]
            for suffix in ('&merchant_id=bad','&date_from=bad','&amount_min=NaN','&page=bad'):
                assert (await c.get(url+suffix)).status_code==400
            assert (await c.get(f'{BASE}/tradespace/control?tab=audit&role=superadmin')).status_code==403
    asyncio.run(scenario())

def test_staff_commands_keep_csrf_and_role_authority(monkeypatch):
    from app.web import routes as legacy
    monkeypatch.setattr(legacy,'enqueue_webhook_delivery',lambda *_:True)
    async def scenario():
        async with fixture() as (data,conn),client() as c:
            await login(c,data['support']);page=await c.get(BASE)
            denied=await c.post(f'{BASE}/deposits/{data["pending"].id}/decline',data={'csrf_token':csrf(page),'reason':'not allowed'})
            assert denied.status_code==403
            assert await conn.scalar(select(m.Deposit.status).where(m.Deposit.id==data['pending'].id))=='pending'
            assert (await c.get(f'{BASE}/tradespace/integrations?tab=credentials&id={data["merchant"].id}')).status_code==403
        async with fixture() as (data,conn),client() as c:
            await login(c,data['admin']);page=await c.get(f'{BASE}/tradespace/network?tab=requisites&id={data["requisite"].id}')
            assert f'action="{BASE}/requisites/{data["requisite"].id}/toggle"' in page.text
            assert (await c.post(f'{BASE}/requisites/{data["requisite"].id}/toggle')).status_code==403
            assert (await c.post(f'{BASE}/requisites/{data["requisite"].id}/toggle',data={'csrf_token':csrf(page)})).status_code==303
            assert await conn.scalar(select(m.Requisite.enabled).where(m.Requisite.id==data['requisite'].id)) is False
            assert await conn.scalar(select(func.count()).select_from(m.AuditLog).where(m.AuditLog.target_id==str(data['requisite'].id),m.AuditLog.action=='requisite_toggled'))==1
            operation=await c.get(f'{BASE}/tradespace/operations/{data["pending"].id}')
            assert f'action="{BASE}/deposits/{data["pending"].id}/decline"' in operation.text
            response=await c.post(f'{BASE}/deposits/{data["pending"].id}/decline',data={'csrf_token':csrf(operation),'reason':'Wave5 rollback-only check'})
            assert response.status_code==303
            assert await conn.scalar(select(m.Deposit.status).where(m.Deposit.id==data['pending'].id))=='failed'
            assert await conn.scalar(select(m.User.trader_hold).where(m.User.id==data['trader'].id))==0
            assert await conn.scalar(select(m.User.trader_balance).where(m.User.id==data['trader'].id))==Decimal('555555.55')
            follow=await c.get(response.headers['location']);assert follow.status_code==200 and 'StaffControlCenter' in follow.text
    asyncio.run(scenario())

def test_staff_form_targets_are_existing_post_commands_and_sensitive_actions_scoped():
    from app.web import routes as legacy
    from starlette.routing import compile_path
    patterns=[compile_path(r.path)[0] for r in legacy.router.routes if 'POST' in (getattr(r,'methods',None) or set())]
    async def scenario():
        async with fixture() as (data,conn),client() as c:
            await login(c,data['super'])
            for section,tab,key in [('network','traders','trader'),('network','merchants','merchant'),('network','teamleads','lead'),('finance','merchants','balance'),('finance','settlements','settlement'),('control','antiscam',None),('control','ai',None)]:
                url=f'{BASE}/tradespace/{section}?tab={tab}'+(f'&id={data[key].id}' if key else '')
                page=await c.get(url);assert page.status_code==200
                actions=re.findall(r'<form method="post" action="([^"]+)"[^>]*>(.*?)</form>',page.text,re.S)
                assert actions
                for path,body in actions:
                    assert 'name="csrf_token"' in body
                    bare=path.removeprefix('/staff')
                    assert any(pattern.match(bare) for pattern in patterns),path
            await c.post('/staff/logout',data={'csrf_token':csrf(page)})
            await login(c,data['admin'])
            detail=await c.get(f'{BASE}/tradespace/network?tab=traders&id={data["trader"].id}')
            assert f'/traders/{data["trader"].id}/finance' not in detail.text
            assert '/antiscam/' not in detail.text and '/users/' not in detail.text
            assert '/commission-tiers' in detail.text
    asyncio.run(scenario())

def test_teamlead_and_non_staff_realms_cannot_enter_staff_workspaces():
    async def scenario():
        async with fixture() as (data,conn),client() as c:
            assert (await c.get(BASE)).status_code==303
            await login(c,data['lead'])
            page=await c.get(BASE);assert page.status_code==200 and 'TeamLead' in page.text and 'StaffControlCenter' not in page.text
            for section in ('center','network','control','integrations'):
                assert (await c.get(f'{BASE}/tradespace/{section}')).status_code==403
            assert (await c.get(f'{BASE}/tradespace/operations/{data["pending"].id}')).status_code==403
        async with fixture() as (data,conn),client() as c:
            await login(c,data['trader'],realm='trader')
            response=await c.get(f'{BASE}/tradespace/network');assert response.status_code in {303,403}
            assert 'StaffControlCenter' not in response.text
            assert (await c.get('/trader/cabinet/tradespace/control')).status_code==403
    asyncio.run(scenario())

def test_staff_credential_issue_and_future_tariff_do_not_leak_or_reprice_history():
    from app.services.fee_tiers import COMMISSION_TIER_RANGES
    async def scenario():
        async with fixture() as (data,conn),client() as c:
            await login(c,data['admin'])
            page=await c.get(f'{BASE}/tradespace/network?tab=merchants&id={data["merchant"].id}')
            issued=await c.post(f'{BASE}/merchants/{data["merchant"].id}/api-keys/issue',data={'csrf_token':csrf(page),'mode':'production'})
            assert issued.status_code==200 and issued.headers['cache-control']=='no-store, max-age=0'
            assert 'is-masked' in issued.text
            secret=re.search(r'id="secret-key"[^>]*>([^<]+)<',issued.text).group(1)
            assert secret.startswith('sk_')
            revisit=await c.get(f'{BASE}/tradespace/integrations?tab=credentials&id={data["merchant"].id}')
            assert secret not in revisit.text and CANARY not in revisit.text
            trader_page=await c.get(f'{BASE}/tradespace/network?tab=traders&id={data["trader"].id}')
            response=await c.post(f'{BASE}/traders/{data["trader"].id}/commission-tiers',data={'csrf_token':csrf(trader_page),'payment_method':'sbp',**{t['field']:'6.00' for t in COMMISSION_TIER_RANGES}})
            assert response.status_code==303
            immutable=(await conn.execute(select(m.OperationFeeSnapshot.merchant_fee_amount,m.OperationFeeSnapshot.executor_fee_amount,m.OperationFeeSnapshot.platform_income_amount).where(m.OperationFeeSnapshot.id==data['snapshot'].id))).one()
            assert tuple(immutable)==(Decimal('15000'),Decimal('7000'),Decimal('8000'))
            rates=(await conn.execute(select(m.FeeRule.rate_percent).where(m.FeeRule.entity_id==data['trader'].id,m.FeeRule.is_active.is_(True)))).scalars().all()
            assert len(rates)==4 and all(r==Decimal('6.00') for r in rates)
    asyncio.run(scenario())


def test_staff_read_details_and_refresh_remain_in_realm_without_writes():
    async def scenario():
        async with fixture() as (data,conn),client() as c:
            await login(c,data['super']);writes=[]
            def observe(_a,_b,statement,*_):
                if statement.lstrip().split(' ',1)[0].upper() in {'UPDATE','INSERT','DELETE'}:writes.append(statement)
            event.listen(engine.sync_engine,'before_cursor_execute',observe)
            try:
                for section,tab,key in [('operations','deposits','expired'),('operations','appeals','appeal'),('network','merchants','merchant'),('network','traders','trader'),('finance','merchants','balance'),('integrations','webhooks','webhook')]:
                    response=await c.get(f'{BASE}/tradespace/{section}?tab={tab}&id={data[key].id}')
                    assert response.status_code==200
                    assert 'href="/cabinet' not in response.text
                assert not writes,writes
            finally:event.remove(engine.sync_engine,'before_cursor_execute',observe)
    asyncio.run(scenario())


def test_staff_finance_form_preserves_assignments_outside_directory():
    from sqlalchemy import update
    async def scenario():
        async with fixture() as (data,conn),client() as c:
            merchant_id=str(data['merchant'].id)
            await conn.execute(update(m.Merchant).where(m.Merchant.id==data['merchant'].id).values(is_archived=True))
            await conn.execute(update(m.User).where(m.User.id==data['trader'].id).values(trader_assigned_merchants=[merchant_id]))
            await login(c,data['super'])
            page=await c.get(f'{BASE}/tradespace/network?tab=traders&id={data["trader"].id}')
            assert f'<option value="{merchant_id}" selected>' in page.text
            fields={'csrf_token':csrf(page),'trader_balance':'555555.55','trader_hold':'950.00','trader_traffic_priority':'50','assigned_merchants':merchant_id}
            result=await c.post(f'{BASE}/traders/{data["trader"].id}/finance',data=fields)
            assert result.status_code==303
            assert await conn.scalar(select(m.User.trader_assigned_merchants).where(m.User.id==data['trader'].id))==[merchant_id]
            assert await conn.scalar(select(m.User.trader_balance).where(m.User.id==data['trader'].id))==Decimal('555555.55')
    asyncio.run(scenario())


def test_new_teamlead_adjustment_uses_existing_command_without_get_creation():
    async def scenario():
        async with fixture() as (data,conn),client() as c:
            async with AsyncSession(bind=conn,expire_on_commit=False,join_transaction_mode='create_savepoint') as db:
                lead=m.User(email='new-staff-qa-'+uuid.uuid4().hex+'@example.test',role='teamlead',password_hash=data['lead'].password_hash,is_active=True)
                db.add(lead);await db.commit()
            await login(c,data['super'])
            page=await c.get(f'{BASE}/tradespace/network?tab=teamleads&id={lead.id}')
            assert page.status_code==200
            assert await conn.scalar(select(m.TeamLeadBalance.id).where(m.TeamLeadBalance.teamlead_id==lead.id)) is None
            form=re.search(r'<form method="post" action="'+BASE+'/teamlead/'+str(lead.id)+r'/adjust"[^>]*>(.*?)</form>',page.text,re.S).group(1)
            key=re.search(r'name="idempotency_key" value="([^"]+)"',form).group(1)
            fields={'csrf_token':csrf(page),'adjustment_type':'available_credit','amount_rub':'100.25','reason':'isolated staff UI check','idempotency_key':key}
            for _ in range(2):
                result=await c.post(f'{BASE}/teamlead/{lead.id}/adjust',data=fields)
                assert result.status_code==303
            assert await conn.scalar(select(m.TeamLeadBalance.available_rub).where(m.TeamLeadBalance.teamlead_id==lead.id))==Decimal('100.25')
            assert await conn.scalar(select(func.count()).select_from(m.TeamLeadLedgerEntry).where(m.TeamLeadLedgerEntry.teamlead_id==lead.id))==1
    asyncio.run(scenario())
