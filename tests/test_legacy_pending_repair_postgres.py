import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.enums import DepositStatus, Role
from app.core.security import encrypt_secret, hash_password
from app.models import (
    AuditLog,
    Balance,
    Deposit,
    FeeRule,
    LedgerEntry,
    Merchant,
    MerchantRollingAccount,
    MerchantRollingLedgerEntry,
    OperationFeeSnapshot,
    Requisite,
    TraderLedgerEntry,
    User,
    WebhookEvent,
)
from app.services.deposit_lifecycle import (
    LegacyPendingRepairBlocked,
    expire_due_deposits,
    repair_legacy_pending_without_hold,
)


def _database_url() -> str:
    url = os.getenv('TEST_DATABASE_URL', '')
    if not url:
        pytest.skip('TEST_DATABASE_URL is required for PostgreSQL repair tests')
    if 'postgresql' not in url:
        pytest.fail('Legacy repair integration tests require PostgreSQL')
    if url.startswith('postgresql://'):
        url = url.replace('postgresql://', 'postgresql+asyncpg://', 1)
    return url


async def _legacy_fixture(
    db: AsyncSession,
    *,
    metadata: dict | None = None,
) -> tuple[Deposit, Merchant, User, User, Requisite, Balance]:
    suffix = uuid.uuid4().hex
    owner = User(
        email=f'legacy-merchant-{suffix}@example.test',
        password_hash=hash_password(f'Password-{suffix}'),
        role=Role.merchant.value,
    )
    actor = User(
        email=f'legacy-superadmin-{suffix}@example.test',
        password_hash=hash_password(f'Password-{suffix}'),
        role=Role.superadmin.value,
        twofa_enabled=True,
    )
    trader = User(
        email=f'legacy-trader-{suffix}@example.test',
        password_hash=hash_password(f'Password-{suffix}'),
        role=Role.trader.value,
        trader_balance=Decimal('0.00'),
        trader_hold=Decimal('0.00'),
    )
    db.add_all([owner, actor, trader])
    await db.flush()
    merchant = Merchant(
        owner_id=owner.id,
        name=f'Legacy repair merchant {suffix}',
    )
    db.add(merchant)
    await db.flush()
    balance = Balance(
        merchant_id=merchant.id,
        currency='RUB',
        available=Decimal('123.45'),
        frozen=Decimal('0.00'),
    )
    requisite = Requisite(
        trader_id=trader.id,
        owner_name='Legacy repair fixture',
        method='sbp',
        value_encrypted=encrypt_secret(f'+7999{suffix[:7]}'),
    )
    db.add_all([balance, requisite])
    await db.flush()
    created_at = datetime.now(timezone.utc) - timedelta(hours=2)
    merchant_rule = FeeRule(
        method='sbp',
        percent=Decimal('10.0000'),
        entity_type='merchant',
        entity_id=merchant.id,
        fee_side='merchant_fee',
        payment_method='sbp',
        currency='RUB',
        min_amount=Decimal('0.00'),
        rate_percent=Decimal('10.0000'),
        effective_from=created_at - timedelta(days=1),
    )
    executor_rule = FeeRule(
        method='sbp',
        percent=Decimal('5.0000'),
        entity_type='trader',
        entity_id=trader.id,
        fee_side='executor_fee',
        payment_method='sbp',
        currency='RUB',
        min_amount=Decimal('0.00'),
        rate_percent=Decimal('5.0000'),
        effective_from=created_at - timedelta(days=1),
    )
    db.add_all([merchant_rule, executor_rule])
    await db.flush()
    deposit = Deposit(
        merchant_id=merchant.id,
        external_id=f'legacy-deposit-{suffix}',
        idempotency_key=f'legacy-deposit-{suffix}',
        amount=Decimal('100000.00'),
        currency='RUB',
        method='sbp',
        status=DepositStatus.pending.value,
        requisites_id=requisite.id,
        created_at=created_at,
        expires_at=created_at + timedelta(minutes=5),
        metadata_json=metadata or {},
    )
    db.add(deposit)
    await db.flush()
    db.add(
        OperationFeeSnapshot(
            deposit_id=deposit.id,
            merchant_id=merchant.id,
            merchant_rate_rule_id=merchant_rule.id,
            merchant_rate_version=1,
            merchant_rate_percent=Decimal('10.0000'),
            merchant_fee_amount=Decimal('10000.00'),
            executor_type='trader',
            executor_id=trader.id,
            executor_rate_rule_id=executor_rule.id,
            executor_rate_version=1,
            executor_rate_percent=Decimal('5.0000'),
            executor_fee_amount=Decimal('5000.00'),
            platform_margin_percent=Decimal('5.0000'),
            platform_income_amount=Decimal('5000.00'),
            calculation_base_amount=Decimal('100000.00'),
            currency='RUB',
            payment_method='sbp',
            rate_snapshot_at=created_at,
            settlement_status='pending',
        )
    )
    await db.flush()
    return deposit, merchant, trader, actor, requisite, balance


def test_legacy_orphan_repair_is_dry_run_first_financially_neutral_and_idempotent():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                deposit, _, trader, actor, _, balance = await _legacy_fixture(db)
                dry_run = await repair_legacy_pending_without_hold(
                    db,
                    deposit_id=deposit.id,
                    actor_id=actor.id,
                    reason='verified localhost legacy orphan',
                    dry_run=True,
                )
                assert dry_run.evidence.category == 'A'
                assert dry_run.evidence.eligible is True
                assert dry_run.changed is False
                assert dry_run.status == DepositStatus.pending.value
                assert (
                    dry_run.financial_fingerprint_before
                    == dry_run.financial_fingerprint_after
                )

                applied = await repair_legacy_pending_without_hold(
                    db,
                    deposit_id=deposit.id,
                    actor_id=actor.id,
                    reason='verified localhost legacy orphan',
                    dry_run=False,
                )
                await db.flush()
                assert applied.changed is True
                assert applied.status == DepositStatus.failed.value
                assert (
                    applied.financial_fingerprint_before
                    == applied.financial_fingerprint_after
                )
                assert deposit.metadata_json['failure_reason'] == (
                    'legacy_missing_hold'
                )
                assert trader.trader_balance == Decimal('0.00')
                assert trader.trader_hold == Decimal('0.00')
                assert balance.available == Decimal('123.45')
                assert balance.frozen == Decimal('0.00')
                assert await db.scalar(
                    select(func.count(TraderLedgerEntry.id)).where(
                        TraderLedgerEntry.trader_id == trader.id
                    )
                ) == 0
                assert await db.scalar(
                    select(func.count(LedgerEntry.id)).where(
                        LedgerEntry.operation_id == deposit.id
                    )
                ) == 0
                assert await db.scalar(
                    select(func.count(AuditLog.id)).where(
                        AuditLog.action
                        == 'legacy_pending_without_hold_repaired',
                        AuditLog.target_id == str(deposit.id),
                    )
                ) == 1
                assert await db.scalar(
                    select(func.count(WebhookEvent.id)).where(
                        WebhookEvent.payload['id'].as_string()
                        == str(deposit.id),
                        WebhookEvent.event_type == 'deposit.failed',
                    )
                ) == 1

                repeated = await repair_legacy_pending_without_hold(
                    db,
                    deposit_id=deposit.id,
                    actor_id=actor.id,
                    reason='verified localhost legacy orphan',
                    dry_run=False,
                )
                assert repeated.changed is False
                assert await db.scalar(
                    select(func.count(AuditLog.id)).where(
                        AuditLog.action
                        == 'legacy_pending_without_hold_repaired',
                        AuditLog.target_id == str(deposit.id),
                    )
                ) == 1
                assert await db.scalar(
                    select(func.count(WebhookEvent.id)).where(
                        WebhookEvent.payload['id'].as_string()
                        == str(deposit.id),
                        WebhookEvent.event_type == 'deposit.failed',
                    )
                ) == 1
                assert await expire_due_deposits(db) == []
                await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_legacy_repair_is_blocked_by_hold_ledger_evidence():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                deposit, _, trader, actor, requisite, _ = (
                    await _legacy_fixture(db)
                )
                db.add(
                    TraderLedgerEntry(
                        trader_id=trader.id,
                        operation_id=requisite.id,
                        entry_type='hold',
                        amount=Decimal('100000.00'),
                        balance_after=Decimal('100000.00'),
                        hold_after=Decimal('100000.00'),
                        idempotency_key=f'fixture-hold-{uuid.uuid4().hex}',
                        description='deposit requisite reserved',
                    )
                )
                await db.flush()
                report = await repair_legacy_pending_without_hold(
                    db,
                    deposit_id=deposit.id,
                    actor_id=actor.id,
                    reason='must remain blocked',
                    dry_run=True,
                )
                assert report.evidence.category == 'B'
                assert 'trader_ledger_evidence' in report.evidence.blockers
                with pytest.raises(LegacyPendingRepairBlocked):
                    await repair_legacy_pending_without_hold(
                        db,
                        deposit_id=deposit.id,
                        actor_id=actor.id,
                        reason='must remain blocked',
                        dry_run=False,
                    )
                await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_legacy_repair_is_blocked_by_merchant_credit():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                deposit, merchant, _, actor, _, _ = await _legacy_fixture(db)
                db.add(
                    LedgerEntry(
                        merchant_id=merchant.id,
                        operation_id=deposit.id,
                        entry_type='credit',
                        amount=Decimal('90000.00'),
                        currency='RUB',
                        description='financial evidence',
                        idempotency_key=f'fixture-credit-{uuid.uuid4().hex}',
                    )
                )
                await db.flush()
                report = await repair_legacy_pending_without_hold(
                    db,
                    deposit_id=deposit.id,
                    actor_id=actor.id,
                    reason='must remain blocked',
                    dry_run=True,
                )
                assert report.evidence.category == 'B'
                assert 'merchant_ledger_evidence' in report.evidence.blockers
                await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_legacy_repair_is_blocked_by_rolling_recovery():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                deposit, merchant, _, actor, _, _ = await _legacy_fixture(db)
                account = MerchantRollingAccount(
                    merchant_id=merchant.id,
                    principal_usdt=Decimal('1.000000'),
                    recovered_usdt=Decimal('1.000000'),
                    outstanding_usdt=Decimal('0.000000'),
                    status='exhausted',
                )
                db.add(account)
                await db.flush()
                db.add(
                    MerchantRollingLedgerEntry(
                        rolling_account_id=account.id,
                        merchant_id=merchant.id,
                        deposit_id=deposit.id,
                        entry_type='recovery',
                        amount_usdt=Decimal('1.000000'),
                        amount_rub=Decimal('100.00'),
                        rate_rub=Decimal('100.00000000'),
                        reason='financial evidence',
                        idempotency_key=f'fixture-recovery-{uuid.uuid4().hex}',
                    )
                )
                await db.flush()
                report = await repair_legacy_pending_without_hold(
                    db,
                    deposit_id=deposit.id,
                    actor_id=actor.id,
                    reason='must remain blocked',
                    dry_run=True,
                )
                assert report.evidence.category == 'B'
                assert 'rolling_ledger_evidence' in report.evidence.blockers
                await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_new_deposit_with_missing_hold_remains_worker_fail_closed():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                deposit, _, _, actor, _, _ = await _legacy_fixture(
                    db,
                    metadata={
                        'trader_hold_amount': '95000.00',
                        'trader_hold_status': 'active',
                    },
                )
                report = await repair_legacy_pending_without_hold(
                    db,
                    deposit_id=deposit.id,
                    actor_id=actor.id,
                    reason='must remain blocked',
                    dry_run=True,
                )
                assert report.evidence.category == 'B'
                assert (
                    'declared_hold_metadata_present'
                    in report.evidence.blockers
                )
                assert await expire_due_deposits(db) == []
                await db.refresh(deposit)
                assert deposit.status == DepositStatus.pending.value
                assert 'failure_reason' not in (deposit.metadata_json or {})
                await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())
