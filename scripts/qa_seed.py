"""Reproducible LOCAL QA dataset. Not included in clean-server release artifacts.

Every money movement uses existing ledger/processing services. The expiration
scenario advances the domain clock, never writes a final status directly.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit
import uuid


def _base58(data):
    alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    number = int.from_bytes(data, 'big')
    result = ''
    while number:
        number, rem = divmod(number, 58)
        result = alphabet[rem] + result
    return '1' * (len(data) - len(data.lstrip(b'\0'))) + result


def synthetic_trc20_address():
    body = bytes([0x41]) + bytes.fromhex('0011223344556677889900112233445566778899')
    checksum = hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4]
    return _base58(body + checksum)


def assert_qa_environment(values):
    url=urlsplit(values.get('DATABASE_URL',''))
    if values.get('ENV') not in {'local','test'} or url.hostname!='127.0.0.1' or url.port!=55432 or url.path not in {'/tradespace_dev','/tradespace_test'}:
        raise RuntimeError('QA seed is restricted to explicitly isolated local dev/test databases')


async def seed(values, local):
    from datetime import datetime,timedelta,timezone
    from decimal import Decimal
    import httpx,pyotp
    from sqlalchemy import select,func,text
    from app.core.security import hash_password,encrypt_secret
    from app.core.merchant_hmac import canonical_request_v2,sign_request_v2
    from app.db.session import AsyncSessionLocal,engine
    from app.main import app
    from app import models as m
    from app.services.ledger import manual_trader_adjustment
    from app.services.deposit_confirmation import confirm_deposit_payment
    from app.services.deposit_lifecycle import expire_due_deposits
    from app.services.teamlead import create_or_replace_assignment,create_or_replace_merchant_assignment,create_teamlead_settlement,reject_teamlead_settlement
    from app.services.rolling import register_rolling_transfer,confirm_rolling_transfer
    from app.services.settlements import create_merchant_settlement,complete_merchant_settlement,reject_merchant_settlement
    from app.services.payouts import complete_payout,release_payout_hold
    from app.services.platform_wallet import set_active_platform_wallet
    from app.services.rapira import get_strict_rolling_ask_quote
    from app.services.finance_reconciliation import reconcile_trader_balance,reconcile_merchant_balance
    access_path=local/'qa-access.json'
    if access_path.exists():access=json.loads(access_path.read_text(encoding='utf8'))
    else:
        access={role:{'email':f'{role}@tradespace.local','password':secrets.token_urlsafe(24),
                      'otp_secret':pyotp.random_base32() if role in {'superadmin','admin','support','teamlead'} else ''}
                for role in ('trader','merchant','teamlead','support','admin','superadmin','operator')}
        for role in ('superadmin','admin','support','operator','merchant'):
            prefix='SUPERADMIN' if role=='superadmin' else 'DEMO_'+role.upper()
            access[role].update(email=values.get(prefix+'_EMAIL',access[role]['email']),password=values.get(prefix+'_PASSWORD',access[role]['password']),otp_secret=values.get(prefix+'_2FA_SECRET',access[role]['otp_secret']))
        access['merchant_api']={'api_key':'pk_qa_'+secrets.token_urlsafe(18),'secret_key':secrets.token_urlsafe(32)}
        access_path.write_text(json.dumps(access,indent=2),encoding='utf8')
    users={};ids={};address=synthetic_trc20_address()
    async with AsyncSessionLocal() as db:
        # Serializes the initializer. Re-run is supported, simultaneous seeds fail clearly.
        assert await db.scalar(text("SELECT pg_try_advisory_xact_lock(hashtext('local-qa-seed'))")), 'QA seed already running'
        for role in ('superadmin','admin','support','teamlead','trader','operator','merchant'):
            creds=access[role];u=await db.scalar(select(m.User).where(m.User.email==creds['email']))
            if not u:
                u=m.User(email=creds['email'],password_hash=hash_password(creds['password']),role=role,twofa_enabled=bool(creds['otp_secret']),twofa_secret=encrypt_secret(creds['otp_secret']) if creds['otp_secret'] else None,trader_traffic_status='active')
                db.add(u);await db.flush()
            if u.role!=role:raise RuntimeError('QA account role collision')
            users[role]=u
        merchant=await db.scalar(select(m.Merchant).where(m.Merchant.owner_id==users['merchant'].id))
        if not merchant:
            merchant=m.Merchant(owner_id=users['merchant'].id,name='Тестовый мерчант TradeSpace',sandbox_mode=True,webhook_url='http://127.0.0.1:18081/webhook')
            db.add(merchant);await db.flush()
            db.add(m.ApiKey(merchant_id=merchant.id,api_key=access['merchant_api']['api_key'],secret_hash=encrypt_secret(access['merchant_api']['secret_key']),mode='sandbox'))
        if not await db.scalar(select(m.TraderLedgerEntry.id).where(m.TraderLedgerEntry.idempotency_key=='qa-opening:balance')):
            await manual_trader_adjustment(db,users['trader'],target_balance=Decimal('1000000'),target_hold=Decimal('0'),idempotency_prefix='qa-opening',reason='QA opening funding')
        for kind,identifier,side,rate in [('merchant',merchant.id,'merchant_fee','10'),('trader',users['trader'].id,'executor_fee','5')]:
            if not await db.scalar(select(m.FeeRule.id).where(m.FeeRule.entity_type==kind,m.FeeRule.entity_id==identifier,m.FeeRule.fee_side==side)):
                db.add(m.FeeRule(entity_type=kind,entity_id=identifier,fee_side=side,method='sbp',payment_method='sbp',currency='RUB',min_amount=Decimal('100'),max_amount=Decimal('150000.01'),percent=Decimal(rate),rate_percent=Decimal(rate),effective_from=datetime.now(timezone.utc)-timedelta(minutes=1)))
        req=await db.scalar(select(m.Requisite).where(m.Requisite.automation_id=='qa-primary'))
        if not req:
            req=m.Requisite(trader_id=users['trader'].id,owner_name='Тестовый получатель',full_name='Тестовый получатель',method='sbp',bank_code='sber',bank_name='Сбербанк',value_encrypted=encrypt_secret('+79990000000'),automation_id='qa-primary',enabled=True,status='active',min_check=Decimal('100'),max_check=Decimal('150000'),daily_limit=Decimal('5000000'),operation_limit=1000,request_count=1000,simultaneous_limit=20)
            db.add(req);await db.flush()
        if not await db.scalar(select(m.TeamLeadTraderAssignment.id).where(m.TeamLeadTraderAssignment.trader_id==users['trader'].id)):
            await create_or_replace_assignment(db,teamlead_id=users['teamlead'].id,trader_id=users['trader'].id,commission_percent=Decimal('1'),actor_id=users['superadmin'].id,reason='Набор тестовых сценариев')
        if not await db.scalar(select(m.TeamLeadMerchantAssignment.id).where(m.TeamLeadMerchantAssignment.merchant_id==merchant.id)):
            await create_or_replace_merchant_assignment(db,teamlead_id=users['teamlead'].id,merchant_id=merchant.id,commission_percent=Decimal('0.5'),actor_id=users['superadmin'].id,reason='Набор тестовых сценариев')
        await set_active_platform_wallet(db,address=address,label='Тестовый адрес — не переводить средства',actor_id=users['superadmin'].id,change_reason='Локальная визуальная проверка QR')
        await db.commit()
        mid=merchant.id;tid=users['trader'].id;staffid=users['superadmin'].id;leadid=users['teamlead'].id;ownerid=users['merchant'].id
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000',headers={'X-Real-IP':'2001:db8::7788'}) as client:
        async def signed(path,payload,key):
            body=json.dumps(payload,separators=(',',':')).encode() if payload is not None else b'';timestamp=str(int(time.time()));nonce=uuid.uuid4().hex
            canonical=canonical_request_v2(timestamp=timestamp,nonce=nonce,method='POST',path=path,query='',content_type='application/json',body=body)
            creds=access['merchant_api'];headers={'X-API-Key':creds['api_key'],'X-Signature-Version':'2','X-Timestamp':timestamp,'X-Nonce':nonce,'X-Signature':sign_request_v2(creds['secret_key'],canonical),'Content-Type':'application/json','Idempotency-Key':key}
            response=await client.post(path,content=body,headers=headers)
            if response.status_code!=200:raise RuntimeError('QA request failed: '+path+' '+response.text)
            return response.json()
        async def deposit(name,amount='1000'):
            external='qa-'+name
            return await signed('/api/v1/merchant/deposits',{'external_id':external,'amount':amount,'currency':'RUB','method':'sbp'},external)
        expired=await deposit('expired');ids['expired']=expired['id']
        if expired['status']=='pending':
            async with AsyncSessionLocal() as db:
                await expire_due_deposits(db,now=datetime.now(timezone.utc)+timedelta(days=1));await db.commit()
        async with AsyncSessionLocal() as db:
            transfer=await db.scalar(select(m.MerchantRollingTransfer).where(m.MerchantRollingTransfer.idempotency_key=='qa-rolling-funded'))
            if not transfer:
                transfer=await register_rolling_transfer(db,merchant_id=mid,actor_id=staffid,amount_usdt=Decimal('100'),network='TRC20',destination_address=address,tx_hash=hashlib.sha256(b'qa-rolling-funded').hexdigest(),sent_at=datetime.now(timezone.utc),comment='Тестовое подтверждение, реального перевода нет',idempotency_key='qa-rolling-funded')
                await db.commit()
                await confirm_rolling_transfer(db,transfer_id=transfer.id,merchant_id=mid,actor_id=ownerid);await db.commit()
        for name in ('paid','paid-second'):
            result=await deposit(name,'100000');ids[name]=result['id']
            if result['status']=='pending':
                async with AsyncSessionLocal() as db:
                    await confirm_deposit_payment(db,uuid.UUID(result['id']),actor_id=tid,actor_ip='127.0.0.1',audit_action='qa_deposit_confirmed',description='Подтверждение тестовой оплаты')
        cancelled=await deposit('cancelled');ids['cancelled']=cancelled['id']
        if cancelled['status']=='pending':await signed('/api/v1/merchant/deposits/qa-cancelled/cancel',None,'qa-cancelled-cancel')
        pending=await deposit('pending');ids['pending']=pending['id']
        appealed=await deposit('appeal');ids['appeal_operation']=appealed['id']
        async with AsyncSessionLocal() as db:appeal=await db.scalar(select(m.Appeal).where(m.Appeal.operation_id==uuid.UUID(appealed['id'])))
        if not appeal:
            creds=access['superadmin'];login=await client.post('/api/v1/auth/login',json={'email':creds['email'],'password':creds['password'],'otp':pyotp.TOTP(creds['otp_secret']).now()});assert login.status_code==200
            response=await client.post('/api/v1/cabinet/appeals',headers={'Authorization':'Bearer '+login.json()['access_token']},json={'operation_type':'deposit','operation_id':appealed['id'],'message':'Тестовое обращение: проверка платёжного подтверждения.'});assert response.status_code==200,response.text
            ids['appeal']=response.json()['id']
        else:ids['appeal']=str(appeal.id)
        for name,status in [('payout-completed','completed'),('payout-rejected','rejected')]:
            result=await signed('/api/v1/merchant/payouts',{'external_id':'qa-'+name,'amount':'100','currency':'RUB','method':'sbp','destination':'Тестовый получатель'},'qa-'+name)
            if result['status']=='pending':
                async with AsyncSessionLocal() as db:
                    payout=await db.scalar(select(m.Payout).where(m.Payout.id==uuid.UUID(result['id'])).with_for_update())
                    if status=='completed':await complete_payout(db,payout)
                    else:await release_payout_hold(db,payout,status,'payout rejected')
                    await db.commit()
            ids[name]=result['id']
    for name,status in [('rejected','rejected'),('completed','completed'),('pending','pending')]:
        async with AsyncSessionLocal() as db:
            row=await db.scalar(select(m.MerchantSettlement).where(m.MerchantSettlement.idempotency_key=='qa-settlement-'+name))
            if not row:
                row=await create_merchant_settlement(db,merchant_id=mid,requested_by_id=ownerid,amount_usdt=Decimal('1'),trc20_address=address,idempotency_key='qa-settlement-'+name);await db.commit()
                if status=='rejected':await reject_merchant_settlement(db,settlement_id=row.id,actor_id=staffid,reason='Тестовое отклонение')
                elif status=='completed':await complete_merchant_settlement(db,settlement_id=row.id,actor_id=staffid,tx_hash=hashlib.sha256(b'qa-settlement-completed').hexdigest())
                await db.commit()
            ids['settlement-'+name]=str(row.id)
    quote=await get_strict_rolling_ask_quote()
    async with AsyncSessionLocal() as db:
        row=await db.scalar(select(m.TeamLeadSettlement).where(m.TeamLeadSettlement.idempotency_key=='qa-teamlead-rejected'))
        if not row:
            row=await create_teamlead_settlement(db,teamlead_id=leadid,requested_usdt=Decimal('1'),wallet_address=address,idempotency_key='qa-teamlead-rejected',quote=quote);await db.commit()
            await reject_teamlead_settlement(db,settlement_id=row.id,actor_id=staffid,reason='Тестовое отклонение');await db.commit()
        ids['teamlead-settlement']=str(row.id)
        tr=await reconcile_trader_balance(db,tid);mr=await reconcile_merchant_balance(db,mid)
        assert tr.ok and mr.ok,(tr,mr)
        counts={cls.__tablename__:await db.scalar(select(func.count()).select_from(cls)) for cls in (m.User,m.Deposit,m.Payout,m.LedgerEntry,m.TraderLedgerEntry,m.MerchantSettlement,m.Appeal,m.OperationFeeSnapshot,m.TeamLeadLedgerEntry,m.MerchantRollingLedgerEntry)}
    (local/'qa-scenarios.json').write_text(json.dumps({'operations':ids,'counts':counts,'trader_reconciled':tr.ok,'merchant_reconciled':mr.ok,'rate_source':'actual read-only Rapira quote'},indent=2),encoding='utf8')
    print(json.dumps({'qa_seed':'PASS','counts':counts,'credentials':'outside repository','reconciliation':'PASS'}))
    await engine.dispose()


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--confirm-local',action='store_true');args=parser.parse_args()
    if not args.confirm_local:raise SystemExit('Explicit --confirm-local required')
    from scripts.recovery_run import local_environment,LOCAL
    values=local_environment();assert_qa_environment(values);os.environ.update(values)
    import psycopg
    with psycopg.connect(values['SYNC_DATABASE_URL'].replace('+psycopg',''),autocommit=True,connect_timeout=5) as lock:
        if not lock.execute("SELECT pg_try_advisory_lock(hashtext('local-qa-initializer'))").fetchone()[0]:
            raise RuntimeError('Another QA initialization is running')
        asyncio.run(seed(values,LOCAL))

if __name__=='__main__':main()
