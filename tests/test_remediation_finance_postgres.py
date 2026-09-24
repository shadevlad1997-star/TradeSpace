"""Targeted local PostgreSQL financial invariants and real browser-route requests."""
import asyncio
import re
import uuid
from decimal import Decimal
import httpx
import pyotp
import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.main import app
from app.models import User,Balance,Merchant,LedgerEntry,TraderLedgerEntry,Deposit,MerchantSettlement
from app.services.ledger import credit,manual_trader_adjustment
from app.services.finance_reconciliation import reconcile_trader_balance,reconcile_merchant_balance
from tests.test_acceptance_financial_postgres import setup,request,state,finish,replay_client_per_event_loop
from tests.test_acceptance_known_gaps import authenticated,PASSWORD,OTP
from tests.test_settlement_hardening_postgres import _quote,VALID_ADDRESS

async def journal_opening(engine,mid,tid):
    async with AsyncSession(engine,expire_on_commit=False) as db:
        trader=await db.get(User,tid);trader.trader_balance=0
        await manual_trader_adjustment(db,trader,target_balance=Decimal('100000'),target_hold=Decimal('0'),idempotency_prefix='remediation-opening',reason='QA opening funding')
        balance=await db.scalar(select(Balance).where(Balance.merchant_id==mid));balance.available=0
        await credit(db,mid,Decimal('1000'),idempotency_key='remediation-merchant-opening',description='QA opening funding')
        await db.commit()


def test_reconcile_active_obligations_detects_missing_hold_even_when_last_ledger_matches():
    async def run():
        engine,key,secret,mid,tid=await setup()
        try:
            await journal_opening(engine,mid,tid)
            r=await request(key,secret,uuid.uuid4().hex);assert r.status_code==200
            async with AsyncSession(engine) as db:
                before=await reconcile_trader_balance(db,tid);assert before.ok,before
                assert before.obligation_frozen==950
            async with AsyncSession(engine) as db:
                trader=await db.get(User,tid);trader.trader_hold=0
                # Isolated corruption: remove reserve evidence too. Old snapshot-only
                # reconciler would say OK, but active obligation remains 950.
                rows=(await db.scalars(select(TraderLedgerEntry).where(TraderLedgerEntry.trader_id==tid,TraderLedgerEntry.entry_type=='hold'))).all()
                for row in rows:await db.delete(row)
                await db.commit()
            async with AsyncSession(engine) as db:
                result=await reconcile_trader_balance(db,tid)
                assert not result.ok and 'active_obligation_reserve_mismatch' in result.issues
                assert result.actual_frozen==0 and result.obligation_frozen==950
                assert result.expected_frozen==0
                assert not db.dirty and not db.new and not db.deleted
        finally:await finish(engine)
    asyncio.run(run())


def test_reconciliation_consistent_snapshot_manual_reserve_and_concurrent_cancel():
    async def run():
        engine,key,secret,mid,tid=await setup()
        try:
            await journal_opening(engine,mid,tid)
            async with AsyncSession(engine,expire_on_commit=False) as db:
                trader=await db.get(User,tid)
                await manual_trader_adjustment(db,trader,target_balance=trader.trader_balance,target_hold=Decimal('300'),idempotency_prefix='insurance',reason='QA manual insurance');await db.commit()
            ext=uuid.uuid4().hex;assert (await request(key,secret,ext)).status_code==200
            reads=[]
            async def sample():
                for _ in range(15):
                    async with AsyncSession(engine) as db:
                        result=await reconcile_trader_balance(db,tid);assert result.ok,result;reads.append(result.obligation_frozen)
            await asyncio.gather(sample(),request(key,secret,ext,cancel=True))
            assert set(reads)<={Decimal('1250'),Decimal('300')}
            async with AsyncSession(engine) as db:
                result=await reconcile_trader_balance(db,tid);assert result.ok and result.obligation_frozen==300
        finally:await finish(engine)
    asyncio.run(run())


def csrf(page):
    found=re.search(r'name="csrf_token"\s+value="([^"]+)"',page.text);assert found;return found.group(1)

async def browser_login(c,user):
    realm='staff' if user.role in {'admin','support','superadmin','teamlead'} else 'merchant'
    page=await c.get('/'+realm+'/login')
    response=await c.post('/'+realm+'/login',data={'email':user.email,'password':PASSWORD,'otp':pyotp.TOTP(OTP).now(),'csrf_token':csrf(page)})
    assert response.status_code==303,response.text
    return realm


def test_settlement_reject_real_browser_post_csrf_roles_repeat_and_reconcile(monkeypatch):
    import app.services.settlements as settlements
    async def quote():return _quote()
    monkeypatch.setattr(settlements,'_live_quote',quote)
    async def run():
        engine,key,secret,mid,tid=await setup()
        try:
            await journal_opening(engine,mid,tid)
            async with AsyncSession(engine,expire_on_commit=False) as db:
                merchant=await db.get(Merchant,mid)
                row=await settlements.create_merchant_settlement(db,merchant_id=mid,requested_by_id=merchant.owner_id,amount_usdt=Decimal('1'),trc20_address=VALID_ADDRESS,idempotency_key='fix-reject')
                await db.commit();sid=row.id;reserve=row.total_debit_rub
            async with AsyncSession(engine) as db:
                result=await reconcile_merchant_balance(db,mid);assert result.ok,result;assert result.obligation_frozen==reserve
            path=f'/staff/cabinet/settlements/{sid}/reject'
            for role in ('support','admin','merchant'):
                async with authenticated(engine,role) as (_,user),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://localhost') as c:
                    realm=await browser_login(c,user);page=await c.get('/'+realm+'/cabinet')
                    denied=await c.post(path,data={'csrf_token':csrf(page),'reject_reason':'Synthetic rejection'})
                    assert denied.status_code in {303,403}
                    async with AsyncSession(engine) as db:assert (await db.get(MerchantSettlement,sid)).status=='pending'
            async with authenticated(engine,'superadmin') as (_,user),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://localhost') as c:
                await browser_login(c,user);page=await c.get('/staff/cabinet')
                denied=await c.post(path,data={'reject_reason':'Synthetic rejection'});assert denied.status_code==403
                for _ in range(2):
                    r=await c.post(path,data={'csrf_token':csrf(page),'reject_reason':'Synthetic rejection'});assert r.status_code==303,r.text
            async with AsyncSession(engine) as db:
                row=await db.get(MerchantSettlement,sid);assert row.status=='rejected'
                result=await reconcile_merchant_balance(db,mid);assert result.ok,result
                assert result.actual_available==1000 and result.actual_frozen==0 and result.obligation_frozen==0
                count=await db.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.operation_id==sid,LedgerEntry.entry_type=='release'));assert count==1
        finally:await finish(engine)
    asyncio.run(run())
