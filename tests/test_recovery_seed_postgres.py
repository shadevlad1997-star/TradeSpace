"""Regression: demo seeding must satisfy the current fee-rule schema."""
import asyncio
import os
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.enums import Role
from app.models import FeeRule, Balance, LedgerEntry
from app.services.fee_tiers import resolve_fee_rule
from scripts.seed import ensure_demo_merchant, ensure_user


def test_demo_merchant_seed_current_fee_schema_and_idempotency():
    url = os.getenv('TEST_DATABASE_URL')
    if not url:
        pytest.skip('TEST_DATABASE_URL is required')

    async def scenario():
        engine = create_async_engine(url)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                owner = await ensure_user(db, f'recovery-seed-{uuid.uuid4().hex}@example.test',
                                          'local-test-only-password', Role.merchant)
                merchant = await ensure_demo_merchant(db, owner)
                await db.commit()
                again = await ensure_demo_merchant(db, owner)
                await db.commit()
                assert merchant.id == again.id
                balance=await db.scalar(select(Balance).where(Balance.merchant_id==merchant.id))
                assert balance.available==0 and balance.frozen==0
                assert await db.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.merchant_id==merchant.id))==0
                count = await db.scalar(select(func.count()).select_from(FeeRule).where(FeeRule.entity_id == merchant.id))
                assert count == 3
                for method, expected in [('sbp', '1.5'), ('c2c', '2'), ('mobile_commerce', '3')]:
                    rule = await resolve_fee_rule(db, entity_type='merchant', entity_id=merchant.id,
                        fee_side='merchant_fee', payment_method=method, currency='RUB', amount=Decimal('1000'))
                    assert rule.rate_percent == Decimal(expected)
        finally:
            await engine.dispose()

    asyncio.run(scenario())
