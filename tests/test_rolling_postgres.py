import asyncio
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pyotp
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.enums import DepositStatus, Role
from app.core.security import encrypt_secret, hash_password
from app.db.session import engine as application_engine
from app.main import app
from app.models import (
    AuditLog,
    Balance,
    Deposit,
    FeeRule,
    LedgerEntry,
    Merchant,
    MerchantRollingAccount,
    MerchantRollingAllocation,
    MerchantRollingLedgerEntry,
    MerchantRollingTransfer,
    MerchantRollingTransferConsumption,
    MerchantSettlement,
    OperationFeeSnapshot,
    User,
)
from app.services.ledger import debit_frozen, release_hold
from app.services.rapira import RollingRapiraQuote
from app.services.rolling import (
    RollingDepositQuote,
    RollingError,
    RollingOwnershipError,
    RollingStateConflict,
    apply_merchant_financing,
    cancel_rolling_transfer,
    confirm_rolling_transfer,
    create_pending_allocation,
    dispute_rolling_transfer,
    reconcile_rolling_account,
    register_rolling_transfer,
    release_pending_allocation,
    rolling_overview,
)
from app.services.settlements import create_merchant_settlement


CONCURRENCY_TIMEOUT_SECONDS = 10


def _database_url() -> str:
    url = os.getenv('TEST_DATABASE_URL', '')
    if not url:
        pytest.skip('TEST_DATABASE_URL is required for PostgreSQL locking tests')
    if 'postgresql' not in url:
        pytest.fail('Rolling integration tests require PostgreSQL')
    if url.startswith('postgresql://'):
        url = url.replace('postgresql://', 'postgresql+asyncpg://', 1)
    return url


async def _concurrent(*awaitables):
    return await asyncio.wait_for(
        asyncio.gather(*awaitables, return_exceptions=True),
        timeout=CONCURRENCY_TIMEOUT_SECONDS,
    )


async def _merchant_fixture(
    db: AsyncSession,
) -> tuple[Merchant, User, User]:
    suffix = uuid.uuid4().hex
    owner = User(
        email=f'merchant-{suffix}@example.test',
        password_hash=hash_password(f'Password-{suffix}'),
        role=Role.merchant.value,
    )
    actor = User(
        email=f'superadmin-{suffix}@example.test',
        password_hash=hash_password(f'Password-{suffix}'),
        role=Role.superadmin.value,
        twofa_enabled=True,
    )
    db.add_all([owner, actor])
    await db.flush()
    merchant = Merchant(
        owner_id=owner.id,
        name=f'Test merchant {suffix}',
    )
    db.add(merchant)
    await db.flush()
    return merchant, owner, actor


async def _register_transfer(
    db: AsyncSession,
    merchant: Merchant,
    actor: User,
    *,
    amount: str = '100.000000',
    sent_at: datetime | None = None,
    idempotency_key: str | None = None,
    tx_hash: str | None = None,
    destination_address: str | None = None,
) -> MerchantRollingTransfer:
    suffix = uuid.uuid4().hex
    return await register_rolling_transfer(
        db,
        merchant_id=merchant.id,
        actor_id=actor.id,
        amount_usdt=Decimal(amount),
        network='TRC20',
        destination_address=destination_address or f'T{suffix[:33]}',
        tx_hash=tx_hash or f'tx-{suffix}',
        sent_at=sent_at or datetime.now(timezone.utc),
        comment='PostgreSQL lifecycle test',
        idempotency_key=idempotency_key or f'transfer-{suffix}',
    )


async def _confirm_transfer(
    db: AsyncSession,
    merchant: Merchant,
    owner: User,
    actor: User,
    *,
    amount: str = '100.000000',
    confirmed_at: datetime | None = None,
) -> MerchantRollingTransfer:
    transfer = await _register_transfer(
        db,
        merchant,
        actor,
        amount=amount,
    )
    return await confirm_rolling_transfer(
        db,
        transfer_id=transfer.id,
        merchant_id=merchant.id,
        actor_id=owner.id,
        confirmed_at=confirmed_at,
    )


def _strict_quote(now: datetime, rate: str = '100') -> RollingRapiraQuote:
    return RollingRapiraQuote(
        symbol='USDT/RUB',
        rate=Decimal(rate),
        side='ask',
        source='rapira_live',
        provider_timestamp=None,
        fetched_at=now,
        freshness_basis='fetched_at',
        stale=False,
        provider_field='askPrice',
    )


async def _deposit_fixture(
    db: AsyncSession,
    merchant: Merchant,
    *,
    created_at: datetime,
    gross: str = '10000.00',
    fee: str = '0.00',
    rate: str = '100',
    eligible_sequence: int | None = None,
) -> tuple[Deposit, OperationFeeSnapshot, MerchantRollingAllocation | None]:
    suffix = uuid.uuid4().hex
    merchant_rate = (
        Decimal(fee) * Decimal('100') / Decimal(gross)
        if Decimal(gross)
        else Decimal('0')
    )
    merchant_rule = FeeRule(
        method='sbp',
        percent=merchant_rate,
        entity_type='merchant',
        entity_id=merchant.id,
        fee_side='merchant_fee',
        payment_method='sbp',
        currency='RUB',
        min_amount=Decimal('0'),
        max_amount=None,
        rate_percent=merchant_rate,
        effective_from=created_at - timedelta(minutes=1),
    )
    executor_rule = FeeRule(
        method='sbp',
        percent=Decimal('0'),
        entity_type='trader',
        entity_id=uuid.uuid4(),
        fee_side='executor_fee',
        payment_method='sbp',
        currency='RUB',
        min_amount=Decimal('0'),
        max_amount=None,
        rate_percent=Decimal('0'),
        effective_from=created_at - timedelta(minutes=1),
    )
    db.add_all([merchant_rule, executor_rule])
    await db.flush()
    deposit = Deposit(
        merchant_id=merchant.id,
        external_id=f'deposit-{suffix}',
        idempotency_key=f'deposit-{suffix}',
        amount=Decimal(gross),
        currency='RUB',
        method='sbp',
        status=DepositStatus.pending.value,
        created_at=created_at,
        expires_at=created_at + timedelta(minutes=15),
        metadata_json={},
    )
    db.add(deposit)
    await db.flush()
    snapshot = OperationFeeSnapshot(
        deposit_id=deposit.id,
        merchant_id=merchant.id,
        merchant_rate_rule_id=merchant_rule.id,
        merchant_rate_version=1,
        merchant_rate_percent=merchant_rate,
        merchant_fee_amount=Decimal(fee),
        executor_type='trader',
        executor_id=executor_rule.entity_id,
        executor_rate_rule_id=executor_rule.id,
        executor_rate_version=1,
        executor_rate_percent=Decimal('0'),
        executor_fee_amount=Decimal('0.00'),
        platform_margin_percent=merchant_rate,
        platform_income_amount=Decimal(fee),
        calculation_base_amount=Decimal(gross),
        currency='RUB',
        payment_method='sbp',
        rate_snapshot_at=created_at,
    )
    db.add(snapshot)
    await db.flush()
    allocation = None
    if eligible_sequence is not None:
        allocation = await create_pending_allocation(
            db,
            deposit=deposit,
            snapshot=snapshot,
            quote=RollingDepositQuote(
                quote=_strict_quote(created_at, rate),
                eligible_transfer_sequence=eligible_sequence,
                rolling_eligible_at=created_at,
            ),
        )
    return deposit, snapshot, allocation


def test_pending_transfer_is_idempotent_and_has_no_financial_effect():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, _, actor = await _merchant_fixture(db)
                key = f'idem-{uuid.uuid4().hex}'
                tx_hash = f'tx-{uuid.uuid4().hex}'
                destination = f'T{uuid.uuid4().hex}'
                sent_at = datetime.now(timezone.utc)
                transfer = await _register_transfer(
                    db,
                    merchant,
                    actor,
                    amount='125.500000',
                    sent_at=sent_at,
                    idempotency_key=key,
                    tx_hash=tx_hash,
                    destination_address=destination,
                )
                repeated = await _register_transfer(
                    db,
                    merchant,
                    actor,
                    amount='125.500000',
                    sent_at=sent_at,
                    idempotency_key=key,
                    tx_hash=tx_hash,
                    destination_address=destination,
                )
                assert repeated.id == transfer.id
                assert transfer.status == 'pending_confirmation'
                assert transfer.recovered_usdt == Decimal('0.000000')
                assert transfer.remaining_usdt == Decimal('0.000000')
                assert await db.scalar(
                    select(func.count(MerchantRollingAccount.id)).where(
                        MerchantRollingAccount.merchant_id == merchant.id
                    )
                ) == 0
                assert await db.scalar(
                    select(func.count(MerchantRollingLedgerEntry.id)).where(
                        MerchantRollingLedgerEntry.merchant_id == merchant.id
                    )
                ) == 0
                overview = await rolling_overview(db, merchant.id)
                assert overview['status'] == 'pending_confirmation'
                assert overview['pending_transfers'] == 1
                assert overview['principal'] == Decimal('0.000000')
                assert await db.scalar(
                    select(func.count(AuditLog.id)).where(
                        AuditLog.action == 'rolling_transfer_registered',
                        AuditLog.target_id == str(transfer.id),
                    )
                ) == 1
                await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_concurrent_transfer_registration_returns_one_idempotent_row():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as setup:
                merchant, _, actor = await _merchant_fixture(setup)
                await setup.commit()
                merchant_id = merchant.id
                actor_id = actor.id

            suffix = uuid.uuid4().hex
            kwargs = {
                'merchant_id': merchant_id,
                'actor_id': actor_id,
                'amount_usdt': Decimal('25.000000'),
                'network': 'TRC20',
                'destination_address': f'T{suffix[:33]}',
                'tx_hash': f'tx-{suffix}',
                'sent_at': datetime.now(timezone.utc),
                'comment': 'concurrent idempotency test',
                'idempotency_key': f'transfer-{suffix}',
            }

            async def register_once():
                async with AsyncSession(
                    engine,
                    expire_on_commit=False,
                ) as db:
                    transfer = await register_rolling_transfer(db, **kwargs)
                    await db.commit()
                    return transfer.id

            results = await _concurrent(register_once(), register_once())
            assert all(not isinstance(row, Exception) for row in results)
            assert results[0] == results[1]
            async with AsyncSession(engine) as db:
                assert await db.scalar(
                    select(func.count(MerchantRollingTransfer.id)).where(
                        MerchantRollingTransfer.idempotency_key
                        == kwargs['idempotency_key']
                    )
                ) == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_transfer_uniqueness_and_per_merchant_sequence_are_database_enforced():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, _, actor = await _merchant_fixture(db)
                tx_hash = f'tx-{uuid.uuid4().hex}'
                first = await _register_transfer(
                    db,
                    merchant,
                    actor,
                    tx_hash=tx_hash,
                )
                second = await _register_transfer(db, merchant, actor)
                assert (first.sequence_no, second.sequence_no) == (1, 2)
                db.add(
                    MerchantRollingTransfer(
                        merchant_id=merchant.id,
                        sequence_no=3,
                        amount_usdt=Decimal('1'),
                        recovered_usdt=Decimal('0'),
                        remaining_usdt=Decimal('0'),
                        network='TRC20',
                        destination_address='Tduplicate',
                        tx_hash=tx_hash,
                        status='pending_confirmation',
                        source='registered',
                        sent_at=datetime.now(timezone.utc),
                        created_by=actor.id,
                        idempotency_key=f'other-{uuid.uuid4().hex}',
                    )
                )
                with pytest.raises(IntegrityError):
                    await db.flush()
                await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_confirmation_checks_ownership_and_increments_aggregates_once():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, owner, actor = await _merchant_fixture(db)
                other_merchant, _, _ = await _merchant_fixture(db)
                transfer = await _register_transfer(
                    db,
                    merchant,
                    actor,
                    amount='250.000000',
                )
                with pytest.raises(RollingOwnershipError):
                    await confirm_rolling_transfer(
                        db,
                        transfer_id=transfer.id,
                        merchant_id=other_merchant.id,
                        actor_id=owner.id,
                    )
                confirmed_at = datetime.now(timezone.utc)
                confirmed = await confirm_rolling_transfer(
                    db,
                    transfer_id=transfer.id,
                    merchant_id=merchant.id,
                    actor_id=owner.id,
                    confirmed_at=confirmed_at,
                )
                repeated = await confirm_rolling_transfer(
                    db,
                    transfer_id=transfer.id,
                    merchant_id=merchant.id,
                    actor_id=owner.id,
                )
                assert repeated.id == confirmed.id
                account = await db.scalar(
                    select(MerchantRollingAccount).where(
                        MerchantRollingAccount.merchant_id == merchant.id
                    )
                )
                assert account.principal_usdt == Decimal('250.000000')
                assert account.outstanding_usdt == Decimal('250.000000')
                assert account.recovered_usdt == Decimal('0.000000')
                assert await db.scalar(
                    select(func.count(MerchantRollingLedgerEntry.id)).where(
                        MerchantRollingLedgerEntry.rolling_transfer_id
                        == transfer.id,
                        MerchantRollingLedgerEntry.entry_type == 'funding',
                    )
                ) == 1
                await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_dispute_and_cancel_never_change_financial_aggregates():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, owner, actor = await _merchant_fixture(db)
                transfer = await _register_transfer(db, merchant, actor)
                disputed = await dispute_rolling_transfer(
                    db,
                    transfer_id=transfer.id,
                    merchant_id=merchant.id,
                    actor_id=owner.id,
                    reason='Средства не поступили',
                )
                assert disputed.status == 'disputed'
                cancelled = await cancel_rolling_transfer(
                    db,
                    transfer_id=transfer.id,
                    actor_id=actor.id,
                    reason='Проверка показала ошибочную отправку',
                )
                assert cancelled.status == 'cancelled'
                assert await db.scalar(
                    select(func.count(MerchantRollingAccount.id)).where(
                        MerchantRollingAccount.merchant_id == merchant.id
                    )
                ) == 0
                assert await db.scalar(
                    select(func.count(MerchantRollingLedgerEntry.id)).where(
                        MerchantRollingLedgerEntry.merchant_id == merchant.id
                    )
                ) == 0
                with pytest.raises(RollingStateConflict):
                    await confirm_rolling_transfer(
                        db,
                        transfer_id=transfer.id,
                        merchant_id=merchant.id,
                        actor_id=owner.id,
                    )
                await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize('opposing_action', ['dispute', 'cancel'])
def test_confirmation_races_have_exactly_one_terminal_result(opposing_action):
    async def setup():
        engine = create_async_engine(_database_url())
        async with AsyncSession(engine, expire_on_commit=False) as db:
            merchant, owner, actor = await _merchant_fixture(db)
            transfer = await _register_transfer(db, merchant, actor)
            await db.commit()
            return engine, merchant.id, owner.id, actor.id, transfer.id

    async def scenario():
        engine, merchant_id, owner_id, actor_id, transfer_id = await setup()
        try:
            async def confirm():
                async with AsyncSession(engine, expire_on_commit=False) as db:
                    row = await confirm_rolling_transfer(
                        db,
                        transfer_id=transfer_id,
                        merchant_id=merchant_id,
                        actor_id=owner_id,
                    )
                    await db.commit()
                    return row.status

            async def oppose():
                async with AsyncSession(engine, expire_on_commit=False) as db:
                    if opposing_action == 'dispute':
                        row = await dispute_rolling_transfer(
                            db,
                            transfer_id=transfer_id,
                            merchant_id=merchant_id,
                            actor_id=owner_id,
                            reason='concurrency test',
                        )
                    else:
                        row = await cancel_rolling_transfer(
                            db,
                            transfer_id=transfer_id,
                            actor_id=actor_id,
                            reason='concurrency test',
                        )
                    await db.commit()
                    return row.status

            results = await _concurrent(confirm(), oppose())
            assert sum(not isinstance(row, Exception) for row in results) == 1
            async with AsyncSession(engine) as db:
                transfer = await db.get(MerchantRollingTransfer, transfer_id)
                assert transfer.status in {
                    'confirmed',
                    'disputed' if opposing_action == 'dispute' else 'cancelled',
                }
                account = await db.scalar(
                    select(MerchantRollingAccount).where(
                        MerchantRollingAccount.merchant_id == merchant_id
                    )
                )
                if transfer.status == 'confirmed':
                    assert account.principal_usdt == Decimal('100.000000')
                else:
                    assert account is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_deposit_created_before_confirmation_goes_to_settle_even_if_paid_later():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, owner, actor = await _merchant_fixture(db)
                before = datetime.now(timezone.utc) - timedelta(minutes=2)
                deposit, snapshot, allocation = await _deposit_fixture(
                    db,
                    merchant,
                    created_at=before,
                    eligible_sequence=None,
                )
                assert allocation is None
                await _confirm_transfer(
                    db,
                    merchant,
                    owner,
                    actor,
                    amount='100.000000',
                    confirmed_at=before + timedelta(minutes=1),
                )
                result = await apply_merchant_financing(
                    db,
                    deposit=deposit,
                    snapshot=snapshot,
                    merchant_payable_rub=Decimal('10000'),
                    description='created before confirmation',
                )
                assert result['financing_route'] == 'settle'
                assert result['settle_credited_rub'] == Decimal('10000.00')
                account = await db.scalar(
                    select(MerchantRollingAccount).where(
                        MerchantRollingAccount.merchant_id == merchant.id
                    )
                )
                assert account.outstanding_usdt == Decimal('100.000000')
                await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_deposit_after_confirmation_uses_rolling_and_pending_transfer_does_not():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, owner, actor = await _merchant_fixture(db)
                confirmed_at = datetime.now(timezone.utc) - timedelta(minutes=1)
                confirmed = await _confirm_transfer(
                    db,
                    merchant,
                    owner,
                    actor,
                    amount='100.000000',
                    confirmed_at=confirmed_at,
                )
                pending = await _register_transfer(
                    db,
                    merchant,
                    actor,
                    amount='900.000000',
                )
                created_at = datetime.now(timezone.utc)
                deposit, snapshot, allocation = await _deposit_fixture(
                    db,
                    merchant,
                    created_at=created_at,
                    eligible_sequence=confirmed.sequence_no,
                )
                assert allocation.eligible_transfer_sequence == confirmed.sequence_no
                result = await apply_merchant_financing(
                    db,
                    deposit=deposit,
                    snapshot=snapshot,
                    merchant_payable_rub=Decimal('10000'),
                    description='eligible after confirmation',
                )
                assert result['rolling_applied_usdt'] == Decimal('100.000000')
                assert result['settle_credited_rub'] == Decimal('0.00')
                assert pending.remaining_usdt == Decimal('0.000000')
                assert pending.status == 'pending_confirmation'
                await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_fifo_can_consume_multiple_transfers_and_cross_into_settle():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, owner, actor = await _merchant_fixture(db)
                first = await _confirm_transfer(
                    db,
                    merchant,
                    owner,
                    actor,
                    amount='100.000000',
                )
                second = await _confirm_transfer(
                    db,
                    merchant,
                    owner,
                    actor,
                    amount='200.000000',
                )
                created_at = datetime.now(timezone.utc)
                deposit, snapshot, _ = await _deposit_fixture(
                    db,
                    merchant,
                    created_at=created_at,
                    gross='35000.00',
                    eligible_sequence=second.sequence_no,
                )
                result = await apply_merchant_financing(
                    db,
                    deposit=deposit,
                    snapshot=snapshot,
                    merchant_payable_rub=Decimal('35000'),
                    description='FIFO crossing',
                )
                assert result['rolling_applied_usdt'] == Decimal('300.000000')
                assert result['rolling_applied_rub'] == Decimal('30000.00')
                assert result['settle_credited_rub'] == Decimal('5000.00')
                assert first.remaining_usdt == Decimal('0.000000')
                assert second.remaining_usdt == Decimal('0.000000')
                rows = (
                    await db.execute(
                        select(MerchantRollingTransferConsumption)
                        .where(
                            MerchantRollingTransferConsumption.deposit_id
                            == deposit.id,
                            MerchantRollingTransferConsumption.entry_type
                            == 'recovery',
                        )
                        .order_by(
                            MerchantRollingTransferConsumption.created_at
                        )
                    )
                ).scalars().all()
                assert [row.amount_usdt for row in rows] == [
                    Decimal('100.000000'),
                    Decimal('200.000000'),
                ]
                assert sum(
                    (row.amount_rub for row in rows),
                    Decimal('0.00'),
                ) == Decimal('30000.00')
                assert result['rolling_applied_rub'] + result[
                    'settle_credited_rub'
                ] == Decimal('35000.00')
                await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_future_topup_is_never_used_by_an_older_deposit():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, owner, actor = await _merchant_fixture(db)
                first = await _confirm_transfer(
                    db,
                    merchant,
                    owner,
                    actor,
                    amount='100.000000',
                )
                old_created_at = datetime.now(timezone.utc)
                old_deposit, old_snapshot, allocation = await _deposit_fixture(
                    db,
                    merchant,
                    created_at=old_created_at,
                    gross='20000.00',
                    eligible_sequence=first.sequence_no,
                )
                topup = await _confirm_transfer(
                    db,
                    merchant,
                    owner,
                    actor,
                    amount='100.000000',
                    confirmed_at=old_created_at + timedelta(seconds=1),
                )
                result = await apply_merchant_financing(
                    db,
                    deposit=old_deposit,
                    snapshot=old_snapshot,
                    merchant_payable_rub=Decimal('20000'),
                    description='old deposit boundary',
                )
                assert allocation.eligible_transfer_sequence == first.sequence_no
                assert result['rolling_applied_usdt'] == Decimal('100.000000')
                assert result['settle_credited_rub'] == Decimal('10000.00')
                assert topup.remaining_usdt == Decimal('100.000000')
                assert await db.scalar(
                    select(func.count(MerchantRollingTransferConsumption.id)).where(
                        MerchantRollingTransferConsumption.deposit_id
                        == old_deposit.id,
                        MerchantRollingTransferConsumption.rolling_transfer_id
                        == topup.id,
                    )
                ) == 0
                await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_exhausted_cycle_routes_new_deposit_to_settle_without_allocation():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, owner, actor = await _merchant_fixture(db)
                transfer = await _confirm_transfer(
                    db,
                    merchant,
                    owner,
                    actor,
                    amount='50.000000',
                )
                created_at = datetime.now(timezone.utc)
                first, first_snapshot, _ = await _deposit_fixture(
                    db,
                    merchant,
                    created_at=created_at,
                    gross='5000.00',
                    eligible_sequence=transfer.sequence_no,
                )
                await apply_merchant_financing(
                    db,
                    deposit=first,
                    snapshot=first_snapshot,
                    merchant_payable_rub=Decimal('5000'),
                    description='exhaust cycle',
                )
                account = await db.scalar(
                    select(MerchantRollingAccount).where(
                        MerchantRollingAccount.merchant_id == merchant.id
                    )
                )
                assert account.status == 'exhausted'
                second, second_snapshot, allocation = await _deposit_fixture(
                    db,
                    merchant,
                    created_at=created_at + timedelta(seconds=1),
                    gross='7000.00',
                    eligible_sequence=None,
                )
                assert allocation is None
                result = await apply_merchant_financing(
                    db,
                    deposit=second,
                    snapshot=second_snapshot,
                    merchant_payable_rub=Decimal('7000'),
                    description='after exhausted',
                )
                assert result['settle_credited_rub'] == Decimal('7000.00')
                await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_future_topup_is_never_used_by_legacy_ineligible_deposit():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            created_at = datetime.now(timezone.utc) - timedelta(minutes=5)
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, owner, actor = await _merchant_fixture(db)
                account = MerchantRollingAccount(
                    merchant_id=merchant.id,
                    principal_usdt=Decimal('0.000000'),
                    recovered_usdt=Decimal('0.000000'),
                    outstanding_usdt=Decimal('0.000000'),
                    status='exhausted',
                )
                db.add(account)
                await db.flush()
                deposit, snapshot, _ = await _deposit_fixture(
                    db,
                    merchant,
                    created_at=created_at,
                    gross='10000.00',
                    eligible_sequence=None,
                )
                allocation = MerchantRollingAllocation(
                    deposit_id=deposit.id,
                    merchant_id=merchant.id,
                    rolling_account_id=account.id,
                    gross_rub=Decimal('10000.00'),
                    merchant_fee_percent_snapshot=Decimal('0.000000'),
                    merchant_fee_rub=Decimal('0.00'),
                    merchant_payable_rub=Decimal('10000.00'),
                    rapira_rate_rub=Decimal('100.00000000'),
                    rapira_rate_symbol='USDT/RUB',
                    rapira_rate_side='ask',
                    rapira_rate_source='rapira_live',
                    rapira_rate_field='askPrice',
                    rapira_rate_updated_at=created_at,
                    rapira_provider_timestamp=None,
                    rapira_fetched_at=created_at,
                    rapira_freshness_basis='fetched_at',
                    merchant_payable_usdt=Decimal('100.000000'),
                    eligibility_status='ineligible',
                    eligible_transfer_sequence=None,
                    rolling_eligible_at=None,
                    eligibility_source='legacy_no_confirmed_funding',
                    status='pending',
                )
                db.add(allocation)
                await db.flush()

                topup = await _confirm_transfer(
                    db,
                    merchant,
                    owner,
                    actor,
                    amount='100.000000',
                    confirmed_at=datetime.now(timezone.utc),
                )
                result = await apply_merchant_financing(
                    db,
                    deposit=deposit,
                    snapshot=snapshot,
                    merchant_payable_rub=Decimal('10000.00'),
                    description='legacy ineligible deposit',
                )
                assert result['financing_route'] == 'settle'
                assert result['rolling_eligible'] is False
                assert allocation.status == 'paid'
                assert allocation.rolling_applied_usdt == Decimal('0.000000')
                assert allocation.settle_credited_rub == Decimal('10000.00')
                assert topup.recovered_usdt == Decimal('0.000000')
                assert topup.remaining_usdt == Decimal('100.000000')
                assert await db.scalar(
                    select(func.count(
                        MerchantRollingTransferConsumption.id
                    )).where(
                        MerchantRollingTransferConsumption.deposit_id
                        == deposit.id
                    )
                ) == 0
                balance = await db.scalar(
                    select(Balance).where(
                        Balance.merchant_id == merchant.id,
                        Balance.currency == 'RUB',
                    )
                )
                assert balance.available == Decimal('10000.00')
                await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_released_allocation_and_pending_topup_do_not_change_aggregates():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, owner, actor = await _merchant_fixture(db)
                confirmed = await _confirm_transfer(
                    db,
                    merchant,
                    owner,
                    actor,
                    amount='100.000000',
                )
                deposit, _, allocation = await _deposit_fixture(
                    db,
                    merchant,
                    created_at=datetime.now(timezone.utc),
                    eligible_sequence=confirmed.sequence_no,
                )
                pending_topup = await _register_transfer(
                    db,
                    merchant,
                    actor,
                    amount='500.000000',
                )
                await release_pending_allocation(
                    db,
                    deposit,
                    reason='deposit_failed',
                )
                account = await db.scalar(
                    select(MerchantRollingAccount).where(
                        MerchantRollingAccount.merchant_id == merchant.id
                    )
                )
                assert allocation.status == 'released'
                assert account.principal_usdt == Decimal('100.000000')
                assert account.outstanding_usdt == Decimal('100.000000')
                assert pending_topup.remaining_usdt == Decimal('0.000000')
                await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_account_aggregates_and_reconciliation_match_transfers_and_consumptions():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, owner, actor = await _merchant_fixture(db)
                first = await _confirm_transfer(
                    db,
                    merchant,
                    owner,
                    actor,
                    amount='75.000000',
                )
                second = await _confirm_transfer(
                    db,
                    merchant,
                    owner,
                    actor,
                    amount='125.000000',
                )
                deposit, snapshot, _ = await _deposit_fixture(
                    db,
                    merchant,
                    created_at=datetime.now(timezone.utc),
                    gross='12000.00',
                    eligible_sequence=second.sequence_no,
                )
                await apply_merchant_financing(
                    db,
                    deposit=deposit,
                    snapshot=snapshot,
                    merchant_payable_rub=Decimal('12000'),
                    description='reconciliation',
                )
                report = await reconcile_rolling_account(db, merchant.id)
                assert report.ok is True
                assert report.expected_principal_usdt == Decimal('200.000000')
                assert report.expected_recovered_usdt == Decimal('120.000000')
                assert report.expected_outstanding_usdt == Decimal('80.000000')
                assert first.remaining_usdt == Decimal('0.000000')
                assert second.remaining_usdt == Decimal('80.000000')
                await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_concurrent_paid_at_outstanding_boundary_consumes_once_and_credits_overflow_once():
    async def setup():
        engine = create_async_engine(_database_url())
        async with AsyncSession(engine, expire_on_commit=False) as db:
            merchant, owner, actor = await _merchant_fixture(db)
            transfer = await _confirm_transfer(
                db,
                merchant,
                owner,
                actor,
                amount='100.000000',
            )
            now = datetime.now(timezone.utc)
            first, first_snapshot, _ = await _deposit_fixture(
                db,
                merchant,
                created_at=now,
                eligible_sequence=transfer.sequence_no,
            )
            second, second_snapshot, _ = await _deposit_fixture(
                db,
                merchant,
                created_at=now + timedelta(microseconds=1),
                eligible_sequence=transfer.sequence_no,
            )
            await db.commit()
            return (
                engine,
                merchant.id,
                (first.id, first_snapshot.id),
                (second.id, second_snapshot.id),
            )

    async def scenario():
        engine, merchant_id, first_ids, second_ids = await setup()
        try:
            async def pay(ids):
                async with AsyncSession(engine, expire_on_commit=False) as db:
                    deposit = await db.get(Deposit, ids[0])
                    snapshot = await db.get(OperationFeeSnapshot, ids[1])
                    result = await apply_merchant_financing(
                        db,
                        deposit=deposit,
                        snapshot=snapshot,
                        merchant_payable_rub=Decimal('10000'),
                        description='concurrent paid',
                    )
                    await db.commit()
                    return result

            results = await _concurrent(pay(first_ids), pay(second_ids))
            assert all(not isinstance(row, Exception) for row in results)
            assert sum(
                row['rolling_applied_usdt'] for row in results
            ) == Decimal('100.000000')
            assert sum(
                row['settle_credited_rub'] for row in results
            ) == Decimal('10000.00')
            async with AsyncSession(engine) as db:
                account = await db.scalar(
                    select(MerchantRollingAccount).where(
                        MerchantRollingAccount.merchant_id == merchant_id
                    )
                )
                assert account.outstanding_usdt == Decimal('0.000000')
                assert account.recovered_usdt == Decimal('100.000000')
                assert await db.scalar(
                    select(func.count(MerchantRollingTransferConsumption.id)).where(
                        MerchantRollingTransferConsumption.merchant_id
                        == merchant_id,
                        MerchantRollingTransferConsumption.entry_type
                        == 'recovery',
                    )
                ) == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_paid_financing_is_idempotent_and_does_not_repeat_recovery_or_settle():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, owner, actor = await _merchant_fixture(db)
                transfer = await _confirm_transfer(
                    db,
                    merchant,
                    owner,
                    actor,
                    amount='50.000000',
                )
                deposit, snapshot, _ = await _deposit_fixture(
                    db,
                    merchant,
                    created_at=datetime.now(timezone.utc),
                    gross='10000.00',
                    eligible_sequence=transfer.sequence_no,
                )
                first = await apply_merchant_financing(
                    db,
                    deposit=deposit,
                    snapshot=snapshot,
                    merchant_payable_rub=Decimal('10000'),
                    description='idempotent paid',
                )
                second = await apply_merchant_financing(
                    db,
                    deposit=deposit,
                    snapshot=snapshot,
                    merchant_payable_rub=Decimal('10000'),
                    description='idempotent paid',
                )
                assert first == second
                assert await db.scalar(
                    select(func.count(MerchantRollingTransferConsumption.id)).where(
                        MerchantRollingTransferConsumption.deposit_id
                        == deposit.id,
                        MerchantRollingTransferConsumption.entry_type
                        == 'recovery',
                    )
                ) == 1
                assert await db.scalar(
                    select(func.count(LedgerEntry.id)).where(
                        LedgerEntry.operation_id == deposit.id,
                        LedgerEntry.entry_type == 'credit',
                    )
                ) == 1
                await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_manual_settle_supports_partial_request_and_one_pending_database_guard(
    monkeypatch,
):
    import app.services.settlements as settlements

    async def quote():
        return _strict_quote(
            datetime.now(timezone.utc),
            '100',
        )

    monkeypatch.setattr(settlements, 'get_strict_rolling_ask_quote', quote)

    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, owner, actor = await _merchant_fixture(db)
                balance = Balance(
                    merchant_id=merchant.id,
                    currency='RUB',
                    available=Decimal('100000.00'),
                    frozen=Decimal('0.00'),
                )
                db.add(balance)
                await db.flush()
                first = await create_merchant_settlement(
                    db,
                    merchant_id=merchant.id,
                    requested_by_id=owner.id,
                    amount_usdt=Decimal('100.00'),
                    trc20_address='TJRabPrwbZy45sbavfcjinPJC18kjpRTv8',
                    idempotency_key=f'settlement:{uuid.uuid4().hex}',
                )
                assert first.total_debit_rub < Decimal('100000.00')
                assert balance.frozen == first.total_debit_rub
                with pytest.raises(
                    ValueError,
                    match='merchant_settlement_pending',
                ):
                    await create_merchant_settlement(
                        db,
                        merchant_id=merchant.id,
                        requested_by_id=owner.id,
                        amount_usdt=Decimal('10.00'),
                        trc20_address='TJRabPrwbZy45sbavfcjinPJC18kjpRTv8',
                        idempotency_key=f'settlement:{uuid.uuid4().hex}',
                    )
                with pytest.raises(IntegrityError):
                    async with db.begin_nested():
                        db.add(
                            MerchantSettlement(
                                merchant_id=merchant.id,
                                requested_by_id=owner.id,
                                amount_usdt=Decimal('10.00'),
                                fee_usdt=Decimal('5.00'),
                                rate_rub=Decimal('100.0000'),
                                amount_rub=Decimal('1000.00'),
                                fee_rub=Decimal('500.00'),
                                total_debit_rub=Decimal('1500.00'),
                                trc20_address=(
                                    'TJRabPrwbZy45sbavfcjinPJC18kjpRTv8'
                                ),
                                idempotency_key=(
                                    f'settlement:{uuid.uuid4().hex}'
                                ),
                                status='pending',
                            )
                        )
                        await db.flush()
                first.status = 'rejected'
                await release_hold(
                    db,
                    merchant.id,
                    first.total_debit_rub,
                    first.id,
                    f'merchant-settlement:{first.id}:release',
                    'test rejection',
                )
                second = await create_merchant_settlement(
                    db,
                    merchant_id=merchant.id,
                    requested_by_id=owner.id,
                    amount_usdt=Decimal('200.00'),
                    trc20_address='TJRabPrwbZy45sbavfcjinPJC18kjpRTv8',
                    idempotency_key=f'settlement:{uuid.uuid4().hex}',
                )
                second.status = 'completed'
                await debit_frozen(
                    db,
                    merchant.id,
                    second.total_debit_rub,
                    second.id,
                    f'merchant-settlement:{second.id}:complete',
                    'test completion',
                )
                assert balance.frozen == Decimal('0.00')
                assert await db.scalar(
                    select(func.count(MerchantSettlement.id)).where(
                        MerchantSettlement.merchant_id == merchant.id,
                        MerchantSettlement.status == 'pending',
                    )
                ) == 0
                await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def _csrf_from_html(response: httpx.Response) -> str:
    match = re.search(
        r'name="csrf_token"\s+value="([^"]+)"',
        response.text,
    )
    assert match, response.text[:500]
    return match.group(1)


def test_route_level_superadmin_registration_and_merchant_confirmation():
    async def scenario():
        engine = create_async_engine(_database_url())
        password = f'Password-{uuid.uuid4().hex}'
        otp_secret = pyotp.random_base32()
        suffix = uuid.uuid4().hex
        try:
            async with AsyncSession(engine, expire_on_commit=False) as setup:
                owner = User(
                    email=f'route-merchant-{suffix}@example.test',
                    password_hash=hash_password(password),
                    role=Role.merchant.value,
                )
                actor = User(
                    email=f'route-superadmin-{suffix}@example.test',
                    password_hash=hash_password(password),
                    role=Role.superadmin.value,
                    twofa_enabled=True,
                    twofa_secret=encrypt_secret(otp_secret),
                )
                setup.add_all([owner, actor])
                await setup.flush()
                merchant = Merchant(
                    owner_id=owner.id,
                    name=f'Route merchant {suffix}',
                )
                setup.add(merchant)
                await setup.commit()

            transport = httpx.ASGITransport(
                app=app,
                raise_app_exceptions=True,
            )
            async with httpx.AsyncClient(
                transport=transport,
                base_url='https://localhost',
                follow_redirects=False,
            ) as staff:
                page = await staff.get('/staff/login')
                csrf = _csrf_from_html(page)
                login = await staff.post(
                    '/staff/login',
                    data={
                        'csrf_token': csrf,
                        'email': actor.email,
                        'password': password,
                        'otp': pyotp.TOTP(otp_secret).now(),
                    },
                )
                assert login.status_code == 303
                cabinet = await staff.get(f'/staff/cabinet/tradespace/network?tab=merchants&id={merchant.id}')
                assert cabinet.status_code == 200
                assert 'Зарегистрировать перевод Rolling' in cabinet.text
                assert f'action="/staff/cabinet/rolling/merchants/{merchant.id}/transfers"' in cabinet.text
                staff_csrf = staff.cookies['processing_staff_csrf']
                transfer_url = (
                    f'/staff/cabinet/rolling/merchants/{merchant.id}/transfers'
                )
                missing_csrf = await staff.post(
                    transfer_url,
                    data={'amount_usdt': '12.5'},
                )
                assert missing_csrf.status_code == 403
                registered = await staff.post(
                    transfer_url,
                    data={
                        'csrf_token': staff_csrf,
                        'amount_usdt': '12.500000',
                        'network': 'TRC20',
                        'destination_address': 'TrouteAddress',
                        'tx_hash': f'route-{suffix}',
                        'sent_at': datetime.now(timezone.utc).isoformat(),
                        'comment': 'route test',
                        'idempotency_key': f'route-transfer-{suffix}',
                    },
                )
                assert registered.status_code == 303

            async with AsyncSession(engine, expire_on_commit=False) as db:
                transfer = await db.scalar(
                    select(MerchantRollingTransfer).where(
                        MerchantRollingTransfer.merchant_id == merchant.id
                    )
                )
                assert transfer.status == 'pending_confirmation'
                transfer_id = transfer.id

            async with httpx.AsyncClient(
                transport=transport,
                base_url='https://localhost',
                follow_redirects=False,
            ) as merchant_client:
                page = await merchant_client.get('/merchant/login')
                csrf = _csrf_from_html(page)
                login = await merchant_client.post(
                    '/merchant/login',
                    data={
                        'csrf_token': csrf,
                        'email': owner.email,
                        'password': password,
                        'otp': '',
                    },
                )
                assert login.status_code == 303
                cabinet = await merchant_client.get(
                    '/merchant/cabinet/tradespace/finance'
                )
                assert cabinet.status_code == 200
                assert 'data-dialog-open="rolling-confirm-' in cabinet.text
                assert 'id="rolling-confirm-' in cabinet.text
                assert 'data-dialog-open="rolling-dispute-' in cabinet.text
                assert 'id="rolling-dispute-' in cabinet.text
                confirm_form_start = cabinet.text.index('id="rolling-confirm-')
                confirm_form_end = cabinet.text.index('id="rolling-dispute-', confirm_form_start)
                confirm_form_block = cabinet.text[confirm_form_start:confirm_form_end]
                assert 'textarea name="reason"' not in confirm_form_block
                dispute_form_start = confirm_form_end
                dispute_form_block = cabinet.text[dispute_form_start:dispute_form_start + 900]
                assert 'textarea name="reason"' in dispute_form_block
                merchant_csrf = merchant_client.cookies[
                    'processing_merchant_csrf'
                ]
                confirmed = await merchant_client.post(
                    (
                        f'/merchant/cabinet/rolling/transfers/'
                        f'{transfer_id}/confirm'
                    ),
                    data={'csrf_token': merchant_csrf},
                )
                assert confirmed.status_code == 303

            async with AsyncSession(engine) as verify:
                transfer = await verify.get(
                    MerchantRollingTransfer,
                    transfer_id,
                )
                assert transfer.status == 'confirmed'
                account = await verify.scalar(
                    select(MerchantRollingAccount).where(
                        MerchantRollingAccount.merchant_id == merchant.id
                    )
                )
                assert account.principal_usdt == Decimal('12.500000')
        finally:
            await application_engine.dispose()
            await engine.dispose()

    asyncio.run(scenario())


def test_source_has_no_persistent_mode_or_automatic_settle_scheduler():
    root = os.path.dirname(os.path.dirname(__file__))
    active_paths = [
        os.path.join(root, 'app', 'core', 'enums.py'),
        os.path.join(root, 'app', 'models', 'entities.py'),
        os.path.join(root, 'app', 'services', 'rolling.py'),
        os.path.join(root, 'app', 'api', 'v1', 'merchant.py'),
        os.path.join(root, 'app', 'web', 'routes.py'),
        os.path.join(root, 'app', 'templates', 'cabinet.html'),
    ]
    prohibited = 'post' + 'paid'
    for path in active_paths:
        text = open(path, encoding='utf-8').read().lower()
        assert prohibited not in text
    route_text = open(
        os.path.join(root, 'app', 'web', 'routes.py'),
        encoding='utf-8',
    ).read()
    template_text = open(
        os.path.join(root, 'app', 'templates', 'cabinet.html'),
        encoding='utf-8',
    ).read()
    assert 'set_merchant_' + 'settlement_' + 'mode' not in route_text
    assert 'name="' + 'settlement_' + 'mode' not in template_text
    celery_text = open(
        os.path.join(root, 'app', 'workers', 'celery_app.py'),
        encoding='utf-8',
    ).read().lower()
    assert 'merchant_settlement_schedule' not in celery_text
    assert 'create_merchant_settlement' not in celery_text
