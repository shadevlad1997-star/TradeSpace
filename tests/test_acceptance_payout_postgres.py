"""Payout terminal money effects under duplicate/concurrent delivery."""
import asyncio
import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.models import Balance, LedgerEntry, Payout
from app.services.payouts import complete_payout, release_payout_hold
from tests.test_acceptance_financial_postgres import setup, finish, replay_client_per_event_loop
from tests.test_merchant_hmac_v2_postgres import _payload, _signed_headers


@pytest.mark.parametrize('outcome',['completed','failed','rejected','cancelled'])
def test_payout_terminal_state_duplicate_and_parallel_execution(outcome):
    async def run():
        engine,key,secret,mid,_=await setup()
        try:
            ext=uuid.uuid4().hex;body=_payload(ext,amount='100')
            headers=_signed_headers(api_key=key,secret=secret,body=body,nonce=uuid.uuid4().hex,idempotency_key=ext)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://localhost') as c:
                response=await c.post('/api/v1/merchant/payouts',content=body,headers=headers)
                assert response.status_code==200,response.text
            oid=uuid.UUID(response.json()['id'])
            async with AsyncSession(engine) as db:
                b=(await db.execute(select(Balance).where(Balance.merchant_id==mid))).scalar_one()
                assert b.available==900 and b.frozen==100
            async def apply():
                async with AsyncSession(engine,expire_on_commit=False) as db:
                    p=(await db.execute(select(Payout).where(Payout.id==oid).with_for_update())).scalar_one()
                    try:
                        if outcome=='completed':await complete_payout(db,p)
                        else:await release_payout_hold(db,p,outcome,'Synthetic audit')
                        await db.commit();return 'applied'
                    except ValueError:
                        await db.rollback();return 'conflict'
            results=await asyncio.wait_for(asyncio.gather(apply(),apply()),15)
            assert sorted(results)==['applied','conflict']
            assert await apply()=='conflict'
            async with AsyncSession(engine) as db:
                p=await db.get(Payout,oid);assert p.status==outcome
                b=(await db.execute(select(Balance).where(Balance.merchant_id==mid))).scalar_one()
                assert b.frozen==0 and b.available==(900 if outcome=='completed' else 1000)
                entries=(await db.execute(select(LedgerEntry).where(LedgerEntry.operation_id==oid))).scalars().all()
                assert sorted((e.entry_type,e.amount) for e in entries)==sorted([('hold',Decimal('100')),('debit' if outcome=='completed' else 'release',Decimal('100'))])
        finally:await finish(engine)
    asyncio.run(run())
