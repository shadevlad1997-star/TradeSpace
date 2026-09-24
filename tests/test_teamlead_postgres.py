import asyncio
import hashlib
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pyotp
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.enums import DepositStatus, Role
from app.core.business_rules import ROLE_PERMISSIONS
from app.core.security import (
    auth_state_marker,
    create_token,
    decrypt_secret,
    encrypt_secret,
    hash_password,
)
from app.core.config import settings
from app.db.session import engine as application_engine
from app.main import app
from app.models import (
    Balance,
    Deposit,
    FeeRule,
    AuditLog,
    Merchant,
    MerchantRollingAllocation,
    OperationFeeSnapshot,
    PlatformLedgerEntry,
    Requisite,
    TeamLeadAccrual,
    TeamLeadBalance,
    TeamLeadLedgerEntry,
    TeamLeadMerchantAccrual,
    TeamLeadMerchantAssignment,
    TeamLeadSettlement,
    TeamLeadTraderAssignment,
    User,
)
from app.services.fees import settle_deposit_credit
from app.services.platform_income import platform_income_dashboard
from app.services.rapira import RollingRapiraQuote
from app.services.teamlead import (
    TeamLeadConflict,
    TeamLeadDebtOutstanding,
    TeamLeadError,
    TeamLeadSettlementUnavailable,
    accrue_teamlead_commission,
    accrue_teamlead_merchant_commission,
    adjust_teamlead_balance,
    close_merchant_assignment,
    complete_teamlead_settlement,
    create_or_replace_assignment,
    create_or_replace_merchant_assignment,
    create_teamlead_settlement,
    reconcile_teamlead_account,
    reject_teamlead_settlement,
    resolve_assignment_for_deposit,
    resolve_merchant_assignment_for_deposit,
    validate_trc20_address,
    reverse_teamlead_accrual,
    reverse_teamlead_accruals_for_deposit,
)


BASE58_ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'


def _base58_encode(value: bytes) -> str:
    if not value:
        return '1'
    num = int.from_bytes(value, byteorder='big')
    encoded = ''
    while num > 0:
        num, remainder = divmod(num, 58)
        encoded = BASE58_ALPHABET[remainder] + encoded
    padding = len(value) - len(value.lstrip(b'\x00'))
    return '1' * padding + encoded


def _base58_decode(value: str) -> bytes:
    num = 0
    for char in value:
        num = num * 58 + BASE58_ALPHABET.index(char)
    byte_len = max(1, (num.bit_length() + 7) // 8)
    decoded = num.to_bytes(byte_len, byteorder='big')
    padding = len(value) - len(value.lstrip('1'))
    return b'\x00' * padding + decoded


def _base58check_address(version: int = 0x41, hash160: bytes | None = None) -> str:
    hash160 = hash160 or (bytes.fromhex('00112233445566778899001122334455667788990011'))
    payload = bytes([version]) + hash160[:20]
    checksum = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    return _base58_encode(payload + checksum)


def _mutate_checksum(wallet_address: str) -> str:
    raw = _base58_decode(wallet_address)
    if len(raw) != 25:
        raise AssertionError('unexpected TRC20 raw payload length')
    mutated = raw[:-4] + b'\x00\x00\x00\x00'
    if mutated == raw:
        mutated = raw[:-4] + b'\x00\x00\x00\x01'
    return _base58_encode(mutated)


def _wrong_network_trc20_address(wallet_address: str | None = None) -> str:
    wallet_address = wallet_address or WALLET
    raw = _base58_decode(wallet_address)
    if len(raw) != 25:
        raise AssertionError('unexpected TRC20 raw payload length')
    return _base58check_address(version=0x42, hash160=raw[1:-4])


WALLET = _base58check_address()


def _database_url() -> str:
    url = os.getenv('TEST_DATABASE_URL', '')
    if not url:
        pytest.skip('TEST_DATABASE_URL is required for TeamLead PostgreSQL tests')
    if 'postgresql' not in url:
        pytest.fail('TeamLead integration tests require PostgreSQL')
    if url.startswith('postgresql://'):
        return url.replace('postgresql://', 'postgresql+asyncpg://', 1)
    return url


def _quote(
    *,
    rate: str = '100',
    at: datetime | None = None,
    provider_timestamp: bool = False,
) -> RollingRapiraQuote:
    at = at or datetime.now(timezone.utc)
    return RollingRapiraQuote(
        symbol='USDT/RUB',
        rate=Decimal(rate),
        side='ask',
        source='rapira_live',
        provider_timestamp=at if provider_timestamp else None,
        fetched_at=at,
        freshness_basis=(
            'provider_timestamp' if provider_timestamp else 'fetched_at'
        ),
        stale=False,
        provider_field='askPrice',
    )


async def _actors(db: AsyncSession):
    suffix = uuid.uuid4().hex
    superadmin = User(
        email=f'super-{suffix}@example.test',
        password_hash=hash_password('superadmin-test-password'),
        role=Role.superadmin.value,
        twofa_enabled=True,
    )
    admin = User(
        email=f'admin-{suffix}@example.test',
        password_hash=hash_password('admin-test-password'),
        role=Role.admin.value,
        twofa_enabled=True,
    )
    teamlead_a = User(
        email=f'teamlead-a-{suffix}@example.test',
        password_hash=hash_password('teamlead-test-password'),
        role=Role.teamlead.value,
        twofa_enabled=True,
    )
    teamlead_b = User(
        email=f'teamlead-b-{suffix}@example.test',
        password_hash=hash_password('teamlead-test-password'),
        role=Role.teamlead.value,
        twofa_enabled=True,
    )
    trader_a = User(
        email=f'trader-a-{suffix}@example.test',
        password_hash=hash_password('trader-test-password'),
        role=Role.operator.value,
        trader_balance=Decimal('12345.67'),
    )
    trader_b = User(
        email=f'trader-b-{suffix}@example.test',
        password_hash=hash_password('trader-test-password'),
        role=Role.trader.value,
    )
    merchant_owner = User(
        email=f'merchant-{suffix}@example.test',
        password_hash=hash_password('merchant-test-password'),
        role=Role.merchant.value,
    )
    db.add_all([
        superadmin,
        admin,
        teamlead_a,
        teamlead_b,
        trader_a,
        trader_b,
        merchant_owner,
    ])
    await db.flush()
    merchant = Merchant(owner_id=merchant_owner.id, name=f'Merchant {suffix}')
    db.add(merchant)
    await db.flush()
    return {
        'superadmin': superadmin,
        'admin': admin,
        'teamlead_a': teamlead_a,
        'teamlead_b': teamlead_b,
        'trader_a': trader_a,
        'trader_b': trader_b,
        'merchant': merchant,
    }


async def _deposit(
    db: AsyncSession,
    actors: dict,
    *,
    trader: User | None = None,
    gross: str = '100000.00',
    created_at: datetime | None = None,
) -> tuple[Deposit, OperationFeeSnapshot]:
    trader = trader or actors['trader_a']
    created_at = created_at or datetime.now(timezone.utc)
    merchant_rule = FeeRule(
        method='sbp',
        percent=Decimal('10.0000'),
        entity_type='merchant',
        entity_id=actors['merchant'].id,
        fee_side='merchant_fee',
        payment_method='sbp',
        currency='RUB',
        min_amount=Decimal('0'),
        rate_percent=Decimal('10.0000'),
        effective_from=created_at - timedelta(days=1),
    )
    trader_rule = FeeRule(
        method='sbp',
        percent=Decimal('5.0000'),
        entity_type='trader',
        entity_id=trader.id,
        fee_side='executor_fee',
        payment_method='sbp',
        currency='RUB',
        min_amount=Decimal('0'),
        rate_percent=Decimal('5.0000'),
        effective_from=created_at - timedelta(days=1),
    )
    db.add_all([merchant_rule, trader_rule])
    await db.flush()
    gross_value = Decimal(gross)
    merchant_fee = (gross_value * Decimal('0.10')).quantize(Decimal('0.01'))
    executor_fee = (gross_value * Decimal('0.05')).quantize(Decimal('0.01'))
    deposit = Deposit(
        merchant_id=actors['merchant'].id,
        external_id=f'teamlead-{uuid.uuid4()}',
        idempotency_key=f'teamlead-{uuid.uuid4()}',
        amount=gross_value,
        currency='RUB',
        method='sbp',
        status=DepositStatus.pending.value,
        expires_at=created_at + timedelta(days=1),
        metadata_json={},
        created_at=created_at,
    )
    db.add(deposit)
    await db.flush()
    snapshot = OperationFeeSnapshot(
        deposit_id=deposit.id,
        merchant_id=actors['merchant'].id,
        merchant_rate_rule_id=merchant_rule.id,
        merchant_rate_version=1,
        merchant_rate_percent=Decimal('10.0000'),
        merchant_fee_amount=merchant_fee,
        executor_type='trader',
        executor_id=trader.id,
        executor_rate_rule_id=trader_rule.id,
        executor_rate_version=1,
        executor_rate_percent=Decimal('5.0000'),
        executor_fee_amount=executor_fee,
        platform_margin_percent=Decimal('5.0000'),
        platform_income_amount=merchant_fee - executor_fee,
        calculation_base_amount=gross_value,
        currency='RUB',
        payment_method='sbp',
        rate_snapshot_at=created_at,
    )
    db.add(snapshot)
    await db.flush()
    return deposit, snapshot


async def _pay(
    db: AsyncSession,
    actors: dict,
    deposit: Deposit,
) -> dict:
    deposit.status = DepositStatus.paid.value
    result = await settle_deposit_credit(
        db,
        merchant_id=actors['merchant'].id,
        method=deposit.method,
        amount=deposit.amount,
        operation_id=deposit.id,
        description='TeamLead PostgreSQL test',
    )
    await db.flush()
    return result


def test_postgres_teamlead_assignment_history_many_traders_and_created_at_attribution():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                actors = await _actors(db)
                base = datetime.now(timezone.utc) - timedelta(days=3)
                first = await create_or_replace_assignment(
                    db,
                    teamlead_id=actors['teamlead_a'].id,
                    trader_id=actors['trader_a'].id,
                    commission_percent=Decimal('0.500000'),
                    actor_id=actors['superadmin'].id,
                    reason='initial assignment',
                    effective_from=base,
                )
                earlier_deposit, _ = await _deposit(
                    db,
                    actors,
                    created_at=base - timedelta(hours=1),
                )
                deposit, _ = await _deposit(
                    db,
                    actors,
                    created_at=base + timedelta(hours=12),
                )
                changed = await create_or_replace_assignment(
                    db,
                    teamlead_id=actors['teamlead_a'].id,
                    trader_id=actors['trader_a'].id,
                    commission_percent=Decimal('0.750000'),
                    actor_id=actors['superadmin'].id,
                    reason='new percent',
                    effective_from=base + timedelta(days=1),
                )
                reassigned = await create_or_replace_assignment(
                    db,
                    teamlead_id=actors['teamlead_b'].id,
                    trader_id=actors['trader_a'].id,
                    commission_percent=Decimal('1.000000'),
                    actor_id=actors['superadmin'].id,
                    reason='reassignment',
                    effective_from=base + timedelta(days=2),
                )
                second_trader = await create_or_replace_assignment(
                    db,
                    teamlead_id=actors['teamlead_b'].id,
                    trader_id=actors['trader_b'].id,
                    commission_percent=Decimal('0.250000'),
                    actor_id=actors['superadmin'].id,
                    reason='second trader',
                    effective_from=base + timedelta(days=2),
                )
                attributed = await resolve_assignment_for_deposit(
                    db,
                    deposit=deposit,
                    trader_id=actors['trader_a'].id,
                )
                assert await resolve_assignment_for_deposit(
                    db,
                    deposit=earlier_deposit,
                    trader_id=actors['trader_a'].id,
                ) is None
                assert attributed.id == first.id
                assert first.effective_to == changed.effective_from
                assert changed.effective_to == reassigned.effective_from
                assert reassigned.effective_to is None
                assert second_trader.teamlead_id == actors['teamlead_b'].id
                assert await db.scalar(
                    select(func.count(TeamLeadTraderAssignment.id)).where(
                        TeamLeadTraderAssignment.trader_id
                        == actors['trader_a'].id,
                        TeamLeadTraderAssignment.effective_to.is_(None),
                    )
                ) == 1
                assert await db.scalar(
                    select(func.count(TeamLeadTraderAssignment.id)).where(
                        TeamLeadTraderAssignment.teamlead_id
                        == actors['teamlead_b'].id,
                        TeamLeadTraderAssignment.effective_to.is_(None),
                    )
                ) == 2
                first.close_reason = 'tampered closed history'
                with pytest.raises(
                    ValueError,
                    match='closed teamlead assignment is immutable',
                ):
                    await db.flush()
                await db.rollback()
        finally:
            await engine.dispose()


def test_postgres_teamlead_deposit_accrual_uses_deposit_created_at():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                actors = await _actors(db)
                cutoff = datetime.now(timezone.utc) - timedelta(days=1)
                await create_or_replace_assignment(
                    db,
                    teamlead_id=actors['teamlead_a'].id,
                    trader_id=actors['trader_a'].id,
                    commission_percent=Decimal('1.000000'),
                    actor_id=actors['superadmin'].id,
                    reason='accrual-attribution',
                    effective_from=cutoff,
                )
                paid_before, _ = await _deposit(
                    db,
                    actors,
                    created_at=cutoff - timedelta(hours=2),
                )
                await _pay(db, actors, paid_before)
                await _pay(
                    db,
                    actors,
                    paid_before,
                )
                assert await db.scalar(
                    select(func.count(TeamLeadAccrual.id)).where(
                        TeamLeadAccrual.deposit_id == paid_before.id
                    )
                ) == 0
                paid_after, _ = await _deposit(
                    db,
                    actors,
                    created_at=cutoff + timedelta(hours=2),
                )
                await _pay(db, actors, paid_after)
                await db.flush()
                after = await db.scalar(
                    select(TeamLeadAccrual).where(
                        TeamLeadAccrual.deposit_id == paid_after.id
                    )
                )
                assert after is not None
                assert after.commission_percent_snapshot == Decimal('1.000000')
                await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_postgres_paid_accrues_once_from_gross_without_changing_trader_merchant_or_rolling(
    caplog,
):
    async def scenario():
        caplog.set_level(logging.WARNING, logger='app.finance')
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                actors = await _actors(db)
                await create_or_replace_assignment(
                    db,
                    teamlead_id=actors['teamlead_a'].id,
                    trader_id=actors['trader_a'].id,
                    commission_percent=Decimal('0.500000'),
                    actor_id=actors['superadmin'].id,
                    reason='gross commission',
                    effective_from=datetime.now(timezone.utc) - timedelta(days=1),
                )
                deposit, snapshot = await _deposit(db, actors)
                trader_before = Decimal(actors['trader_a'].trader_balance)
                assert await accrue_teamlead_commission(
                    db,
                    deposit=deposit,
                    snapshot=snapshot,
                    platform_income_rub=snapshot.platform_income_amount,
                ) is None
                first = await _pay(db, actors, deposit)
                second = await settle_deposit_credit(
                    db,
                    merchant_id=actors['merchant'].id,
                    method=deposit.method,
                    amount=deposit.amount,
                    operation_id=deposit.id,
                    description='idempotent repeat',
                )
                await db.flush()
                accrual = (await db.execute(
                    select(TeamLeadAccrual).where(
                        TeamLeadAccrual.deposit_id == deposit.id
                    )
                )).scalar_one()
                balance = (await db.execute(
                    select(TeamLeadBalance).where(
                        TeamLeadBalance.teamlead_id == actors['teamlead_a'].id
                    )
                )).scalar_one()
                merchant_balance = (await db.execute(
                    select(Balance).where(
                        Balance.merchant_id == actors['merchant'].id
                    )
                )).scalar_one()
                assert (
                    first['merchant_payable_amount']
                    == second['merchant_payable_amount']
                    == '90000.00'
                )
                assert (
                    first['merchant_net_amount']
                    == second['merchant_net_amount']
                    == '90000.00'
                )
                assert accrual.gross_rub == Decimal('100000.00')
                assert accrual.commission_percent_snapshot == Decimal('0.500000')
                assert accrual.accrual_rub == Decimal('500.00')
                assert balance.available_rub == Decimal('500.00')
                assert actors['trader_a'].trader_balance == trader_before
                assert merchant_balance.available == Decimal('90000.00')
                assert await db.scalar(
                    select(func.count(TeamLeadAccrual.id)).where(
                        TeamLeadAccrual.deposit_id == deposit.id
                    )
                ) == 1
                assert await db.scalar(
                    select(func.count(PlatformLedgerEntry.id)).where(
                        PlatformLedgerEntry.operation_id == deposit.id,
                        PlatformLedgerEntry.entry_type == 'teamlead_expense',
                        PlatformLedgerEntry.amount == Decimal('500.00'),
                    )
                ) == 1
                assert await db.scalar(
                    select(func.count(MerchantRollingAllocation.id)).where(
                        MerchantRollingAllocation.deposit_id == deposit.id
                    )
                ) == 0
                change_at = datetime.now(timezone.utc) + timedelta(seconds=1)
                await create_or_replace_assignment(
                    db,
                    teamlead_id=actors['teamlead_a'].id,
                    trader_id=actors['trader_a'].id,
                    commission_percent=Decimal('6.000000'),
                    actor_id=actors['superadmin'].id,
                    reason='loss-making operation test',
                    effective_from=change_at,
                )
                loss_deposit, _ = await _deposit(
                    db,
                    actors,
                    created_at=change_at + timedelta(seconds=1),
                )
                await _pay(db, actors, loss_deposit)
                loss_accrual = await db.scalar(
                    select(TeamLeadAccrual).where(
                        TeamLeadAccrual.deposit_id == loss_deposit.id
                    )
                )
                assert loss_deposit.status == DepositStatus.paid.value
                assert loss_accrual.accrual_rub == Decimal('6000.00')
                assert accrual.accrual_rub == Decimal('500.00')
                assert actors['trader_a'].trader_balance == trader_before
                assert await db.scalar(
                    select(func.count(PlatformLedgerEntry.id)).where(
                        PlatformLedgerEntry.operation_id == loss_deposit.id,
                        PlatformLedgerEntry.entry_type == 'teamlead_expense',
                        PlatformLedgerEntry.amount == Decimal('6000.00'),
                    )
                ) == 1
                assert await db.scalar(
                    select(func.count(AuditLog.id)).where(
                        AuditLog.action == 'teamlead_loss_making_operation',
                        AuditLog.target_id == str(loss_deposit.id),
                    )
                ) == 1
                assert any(
                    row.getMessage() == 'teamlead_loss_making_operation'
                    for row in caplog.records
                )
                income_report = await platform_income_dashboard(
                    db,
                    merchant_id=actors['merchant'].id,
                )
                totals = income_report['totals']
                assert totals['platform_income'] == Decimal('10000.00')
                assert totals['expected_teamlead_expense'] == Decimal('6500.00')
                assert totals['teamlead_expense'] == Decimal('6500.00')
                assert totals['net_platform_income'] == Decimal('3500.00')
                assert totals['net_snapshot_platform_income'] == Decimal('3500.00')
                assert totals['net_ledger_platform_income'] == Decimal('3500.00')
                assert totals['reconciled'] is True

                expense_entry = await db.scalar(
                    select(PlatformLedgerEntry).where(
                        PlatformLedgerEntry.operation_id == deposit.id,
                        PlatformLedgerEntry.entry_type == 'teamlead_expense',
                    )
                )
                await db.delete(expense_entry)
                await db.flush()
                broken_report = await platform_income_dashboard(
                    db,
                    merchant_id=actors['merchant'].id,
                )
                broken_totals = broken_report['totals']
                assert (
                    broken_totals['teamlead_expense_gross_delta']
                    == Decimal('500.00')
                )
                assert (
                    broken_totals['teamlead_expense_reconciliation_delta']
                    == Decimal('500.00')
                )
                assert broken_totals['reconciliation_delta'] == Decimal('-500.00')
                assert broken_totals['reconciled'] is False
                await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_postgres_reversal_creates_debt_future_accrual_offsets_it_and_reconciles():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                actors = await _actors(db)
                await create_or_replace_assignment(
                    db,
                    teamlead_id=actors['teamlead_a'].id,
                    trader_id=actors['trader_a'].id,
                    commission_percent=Decimal('0.500000'),
                    actor_id=actors['superadmin'].id,
                    reason='debt scenario',
                    effective_from=datetime.now(timezone.utc) - timedelta(days=1),
                )
                deposit, _ = await _deposit(db, actors)
                await _pay(db, actors, deposit)
                now = datetime.now(timezone.utc)
                settlement = await create_teamlead_settlement(
                    db,
                    teamlead_id=actors['teamlead_a'].id,
                    requested_usdt=Decimal('100'),
                    wallet_address=WALLET,
                    idempotency_key=f'settle-{uuid.uuid4()}',
                    quote=_quote(rate='1', at=now),
                    now=now,
                )
                await complete_teamlead_settlement(
                    db,
                    settlement_id=settlement.id,
                    actor_id=actors['superadmin'].id,
                    tx_hash=f'tx-{uuid.uuid4()}',
                    now=now,
                )
                reversal = await reverse_teamlead_accrual(
                    db,
                    deposit_id=deposit.id,
                    actor_id=actors['superadmin'].id,
                    reason='paid operation reversed',
                    now=now,
                )
                repeated = await reverse_teamlead_accrual(
                    db,
                    deposit_id=deposit.id,
                    actor_id=actors['superadmin'].id,
                    reason='repeat must be idempotent',
                    now=now,
                )
                assert reversal.id == repeated.id
                balance = await db.scalar(
                    select(TeamLeadBalance).where(
                        TeamLeadBalance.teamlead_id == actors['teamlead_a'].id
                    )
                )
                assert balance.available_rub == Decimal('0.00')
                assert balance.debt_rub == Decimal('105.00')
                with pytest.raises(TeamLeadDebtOutstanding):
                    await create_teamlead_settlement(
                        db,
                        teamlead_id=actors['teamlead_a'].id,
                        requested_usdt=Decimal('1'),
                        wallet_address=WALLET,
                        idempotency_key=f'debt-block-{uuid.uuid4()}',
                        quote=_quote(rate='1', at=now),
                        now=now,
                    )
                future, _ = await _deposit(db, actors)
                await _pay(db, actors, future)
                future_accrual = await db.scalar(
                    select(TeamLeadAccrual).where(
                        TeamLeadAccrual.deposit_id == future.id
                    )
                )
                assert future_accrual.applied_to_debt_rub == Decimal('105.00')
                assert future_accrual.credited_to_available_rub == Decimal('395.00')
                assert balance.debt_rub == Decimal('0.00')
                assert balance.available_rub == Decimal('395.00')
                assert await db.scalar(
                    select(func.count(TeamLeadLedgerEntry.id)).where(
                        TeamLeadLedgerEntry.deposit_id == deposit.id,
                        TeamLeadLedgerEntry.entry_type == 'accrual_reversal',
                    )
                ) == 1
                assert await db.scalar(
                    select(func.count(PlatformLedgerEntry.id)).where(
                        PlatformLedgerEntry.operation_id == deposit.id,
                        PlatformLedgerEntry.entry_type
                        == 'teamlead_expense_reversal',
                    )
                ) == 1
                report = await reconcile_teamlead_account(
                    db, actors['teamlead_a'].id
                )
                assert report['reconciled'], report
                reversal.reversal_reason = 'tampered reversal'
                with pytest.raises(
                    ValueError,
                    match='reversed teamlead accrual is immutable',
                ):
                    await db.flush()
                await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_postgres_settlement_fee_freeze_reject_complete_and_168_hour_cooldown():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                actors = await _actors(db)
                await adjust_teamlead_balance(
                    db,
                    teamlead_id=actors['teamlead_a'].id,
                    actor_id=actors['superadmin'].id,
                    adjustment_type='available_credit',
                    amount_rub=Decimal('50000'),
                    idempotency_key=f'adjust-{uuid.uuid4()}',
                    reason='settlement lifecycle fixture',
                )
                now = datetime.now(timezone.utc)
                key = f'settle-{uuid.uuid4()}'
                first = await create_teamlead_settlement(
                    db,
                    teamlead_id=actors['teamlead_a'].id,
                    requested_usdt=Decimal('100'),
                    wallet_address=WALLET,
                    idempotency_key=key,
                    quote=_quote(rate='100', at=now, provider_timestamp=True),
                    now=now,
                )
                repeated = await create_teamlead_settlement(
                    db,
                    teamlead_id=actors['teamlead_a'].id,
                    requested_usdt=Decimal('100'),
                    wallet_address=WALLET,
                    idempotency_key=key,
                    quote=_quote(rate='100', at=now),
                    now=now,
                )
                balance = await db.scalar(
                    select(TeamLeadBalance).where(
                        TeamLeadBalance.teamlead_id == actors['teamlead_a'].id
                    )
                )
                assert first.id == repeated.id
                assert first.fee_usdt == Decimal('5.000000')
                assert first.total_debit_usdt == Decimal('105.000000')
                assert first.total_debit_rub == Decimal('10500.00')
                assert first.provider_timestamp == now
                assert first.freshness_basis == 'provider_timestamp'
                assert balance.available_rub == Decimal('39500.00')
                assert balance.frozen_rub == Decimal('10500.00')
                with pytest.raises(TeamLeadConflict, match='pending'):
                    await create_teamlead_settlement(
                        db,
                        teamlead_id=actors['teamlead_a'].id,
                        requested_usdt=Decimal('1'),
                        wallet_address=WALLET,
                        idempotency_key=f'second-{uuid.uuid4()}',
                        quote=_quote(rate='100', at=now),
                        now=now,
                    )
                rejected = await reject_teamlead_settlement(
                    db,
                    settlement_id=first.id,
                    actor_id=actors['superadmin'].id,
                    reason='test rejection',
                    now=now,
                )
                repeated_reject = await reject_teamlead_settlement(
                    db,
                    settlement_id=first.id,
                    actor_id=actors['superadmin'].id,
                    reason='ignored repeat',
                    now=now,
                )
                assert rejected.id == repeated_reject.id
                assert balance.available_rub == Decimal('50000.00')
                assert balance.frozen_rub == Decimal('0.00')
                second = await create_teamlead_settlement(
                    db,
                    teamlead_id=actors['teamlead_a'].id,
                    requested_usdt=Decimal('100'),
                    wallet_address=WALLET,
                    idempotency_key=f'after-reject-{uuid.uuid4()}',
                    quote=_quote(rate='100', at=now),
                    now=now,
                )
                with pytest.raises(TeamLeadError, match='tx_hash_required'):
                    await complete_teamlead_settlement(
                        db,
                        settlement_id=second.id,
                        actor_id=actors['superadmin'].id,
                        tx_hash='',
                        now=now,
                    )
                completed = await complete_teamlead_settlement(
                    db,
                    settlement_id=second.id,
                    actor_id=actors['superadmin'].id,
                    tx_hash=f'tx-{uuid.uuid4()}',
                    now=now,
                )
                repeated_complete = await complete_teamlead_settlement(
                    db,
                    settlement_id=second.id,
                    actor_id=actors['superadmin'].id,
                    tx_hash='different-repeat-tx',
                    now=now,
                )
                assert completed.id == repeated_complete.id
                assert completed.completed_at == now
                assert balance.frozen_rub == Decimal('0.00')
                assert balance.total_paid_rub == Decimal('10000.00')
                with pytest.raises(TeamLeadConflict, match='cooldown'):
                    before_cooldown = now + timedelta(hours=167, minutes=59)
                    await create_teamlead_settlement(
                        db,
                        teamlead_id=actors['teamlead_a'].id,
                        requested_usdt=Decimal('1'),
                        wallet_address=WALLET,
                        idempotency_key=f'cooldown-{uuid.uuid4()}',
                        quote=_quote(rate='100', at=before_cooldown),
                        now=before_cooldown,
                    )
                available_at = now + timedelta(hours=168)
                after_cooldown = await create_teamlead_settlement(
                    db,
                    teamlead_id=actors['teamlead_a'].id,
                    requested_usdt=Decimal('1'),
                    wallet_address=WALLET,
                    idempotency_key=f'after-cooldown-{uuid.uuid4()}',
                    quote=_quote(rate='100', at=available_at),
                    now=available_at,
                )
                assert after_cooldown.status == 'pending'
                assert await db.scalar(
                    select(func.count(TeamLeadLedgerEntry.id)).where(
                        TeamLeadLedgerEntry.settlement_id == first.id,
                        TeamLeadLedgerEntry.entry_type == 'settlement_freeze',
                    )
                ) == 1
                assert await db.scalar(
                    select(func.count(TeamLeadLedgerEntry.id)).where(
                        TeamLeadLedgerEntry.settlement_id == first.id,
                        TeamLeadLedgerEntry.entry_type == 'settlement_release',
                    )
                ) == 1
                report = await reconcile_teamlead_account(
                    db, actors['teamlead_a'].id
                )
                assert report['reconciled'], report
                assert report['totals']['manual_net_rub'] == Decimal('50000.00')
                await db.rollback()
        finally:
            await engine.dispose()


def test_postgres_settlement_with_fetched_at_basis_and_stale_rejection():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                actors = await _actors(db)
                await adjust_teamlead_balance(
                    db,
                    teamlead_id=actors['teamlead_a'].id,
                    actor_id=actors['superadmin'].id,
                    adjustment_type='available_credit',
                    amount_rub=Decimal('50000'),
                    idempotency_key=f'adjust-{uuid.uuid4()}',
                    reason='fetched-at quote test',
                )
                now = datetime.now(timezone.utc)
                settlement = await create_teamlead_settlement(
                    db,
                    teamlead_id=actors['teamlead_a'].id,
                    requested_usdt=Decimal('1'),
                    wallet_address=WALLET,
                    idempotency_key=f'settle-fetched-{uuid.uuid4()}',
                    quote=_quote(rate='1.005', at=now),
                    now=now,
                )
                balance = await db.get(TeamLeadBalance, settlement.teamlead_id)
                assert settlement.provider_timestamp is None
                assert settlement.freshness_basis == 'fetched_at'
                assert settlement.fetched_at == now
                assert settlement.requested_rub == Decimal('1.01')
                assert settlement.fee_rub == Decimal('5.03')
                assert settlement.total_debit_rub == Decimal('6.04')
                assert (
                    settlement.total_debit_rub
                    == settlement.requested_rub + settlement.fee_rub
                )
                assert balance.available_rub == Decimal('49993.96')
                assert balance.frozen_rub == Decimal('6.04')
                stale_at = now - timedelta(
                    seconds=settings.ROLLING_RAPIRA_MAX_AGE_SECONDS + 10
                )
                with pytest.raises(TeamLeadSettlementUnavailable):
                    await create_teamlead_settlement(
                        db,
                        teamlead_id=actors['teamlead_b'].id,
                        requested_usdt=Decimal('1'),
                        wallet_address=WALLET,
                        idempotency_key=f'settle-stale-{uuid.uuid4()}',
                        quote=_quote(rate='100', at=stale_at),
                        now=now,
                    )
                await db.rollback()
        finally:
            await engine.dispose()


def test_postgres_settlement_complete_uses_requested_rub_and_tx_hash_unique_for_completed():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                actors = await _actors(db)
                await adjust_teamlead_balance(
                    db,
                    teamlead_id=actors['teamlead_a'].id,
                    actor_id=actors['superadmin'].id,
                    adjustment_type='available_credit',
                    amount_rub=Decimal('50000'),
                    idempotency_key=f'adjust-a-{uuid.uuid4()}',
                    reason='tx-hash unique test',
                )
                await adjust_teamlead_balance(
                    db,
                    teamlead_id=actors['teamlead_b'].id,
                    actor_id=actors['superadmin'].id,
                    adjustment_type='available_credit',
                    amount_rub=Decimal('50000'),
                    idempotency_key=f'adjust-b-{uuid.uuid4()}',
                    reason='tx-hash unique test',
                )
                now = datetime.now(timezone.utc)
                shared_tx = f'tx-{uuid.uuid4()}'
                first = await create_teamlead_settlement(
                    db,
                    teamlead_id=actors['teamlead_a'].id,
                    requested_usdt=Decimal('100'),
                    wallet_address=WALLET,
                    idempotency_key=f'settle-a-{uuid.uuid4()}',
                    quote=_quote(rate='100', at=now),
                    now=now,
                )
                completed = await complete_teamlead_settlement(
                    db,
                    settlement_id=first.id,
                    actor_id=actors['superadmin'].id,
                    tx_hash=shared_tx,
                    now=now,
                )
                balance_a = (
                    await db.execute(
                        select(TeamLeadBalance).where(
                            TeamLeadBalance.teamlead_id == actors['teamlead_a'].id
                        )
                    )
                ).scalar_one()
                assert completed.total_debit_rub == Decimal('10500.00')
                assert balance_a.total_paid_rub == Decimal('10000.00')
                ledger_complete_amount = await db.scalar(
                    select(func.count(TeamLeadLedgerEntry.id)).where(
                        TeamLeadLedgerEntry.settlement_id == first.id,
                        TeamLeadLedgerEntry.entry_type == 'settlement_complete',
                    )
                )
                assert ledger_complete_amount == 1
                second = await create_teamlead_settlement(
                    db,
                    teamlead_id=actors['teamlead_b'].id,
                    requested_usdt=Decimal('100'),
                    wallet_address=WALLET,
                    idempotency_key=f'settle-b-{uuid.uuid4()}',
                    quote=_quote(rate='100', at=now),
                    now=now,
                )
                with pytest.raises(TeamLeadConflict, match='teamlead_settlement_tx_hash_duplicate'):
                    await complete_teamlead_settlement(
                        db,
                        settlement_id=second.id,
                        actor_id=actors['superadmin'].id,
                        tx_hash=shared_tx,
                        now=now,
                    )
                assert (
                    (
                        await db.execute(
                            select(TeamLeadBalance).where(
                                TeamLeadBalance.teamlead_id
                                == actors['teamlead_b'].id
                            )
                        )
                    ).scalar_one()
                    .total_paid_rub
                    == Decimal('0.00')
                )
                completed.tx_hash = f'tampered-{uuid.uuid4()}'
                with pytest.raises(
                    ValueError,
                    match='final teamlead settlement is immutable',
                ):
                    await db.flush()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_postgres_validate_trc20_address_base58check_and_network():
    assert validate_trc20_address(WALLET) == WALLET
    with pytest.raises(TeamLeadError, match='invalid_trc20_address'):
        validate_trc20_address(_mutate_checksum(WALLET))
    with pytest.raises(TeamLeadError, match='invalid_trc20_network'):
        validate_trc20_address(_wrong_network_trc20_address())


def test_teamlead_permission_map_keeps_admin_out_of_finance_and_reassignment():
    assert 'teamlead.manage' in ROLE_PERMISSIONS[Role.superadmin.value]
    assert 'teamlead.finance' in ROLE_PERMISSIONS[Role.superadmin.value]
    assert 'teamlead.create' in ROLE_PERMISSIONS[Role.admin.value]
    assert 'teamlead.read_basic' in ROLE_PERMISSIONS[Role.admin.value]
    assert 'teamlead.manage' not in ROLE_PERMISSIONS[Role.admin.value]
    assert 'teamlead.finance' not in ROLE_PERMISSIONS[Role.admin.value]


def test_postgres_concurrent_settlement_paid_and_final_processing_are_single_effect():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as setup:
                actors = await _actors(setup)
                await adjust_teamlead_balance(
                    setup,
                    teamlead_id=actors['teamlead_a'].id,
                    actor_id=actors['superadmin'].id,
                    adjustment_type='available_credit',
                    amount_rub=Decimal('50000'),
                    idempotency_key=f'adjust-{uuid.uuid4()}',
                    reason='concurrency fixture',
                )
                await create_or_replace_assignment(
                    setup,
                    teamlead_id=actors['teamlead_a'].id,
                    trader_id=actors['trader_a'].id,
                    commission_percent=Decimal('0.500000'),
                    actor_id=actors['superadmin'].id,
                    reason='concurrency assignment',
                    effective_from=datetime.now(timezone.utc) - timedelta(days=1),
                )
                await create_or_replace_merchant_assignment(
                    setup,
                    teamlead_id=actors['teamlead_b'].id,
                    merchant_id=actors['merchant'].id,
                    commission_percent=Decimal('0.750000'),
                    actor_id=actors['superadmin'].id,
                    reason='concurrency merchant assignment',
                    valid_from=datetime.now(timezone.utc) - timedelta(days=1),
                )
                deposit, _ = await _deposit(setup, actors)
                ids = {
                    key: value.id
                    for key, value in actors.items()
                    if key != 'merchant'
                }
                ids['merchant'] = actors['merchant'].id
                ids['deposit'] = deposit.id
                await setup.commit()

            now = datetime.now(timezone.utc)

            async def request_settle(number: int):
                async with AsyncSession(engine, expire_on_commit=False) as db:
                    try:
                        row = await create_teamlead_settlement(
                            db,
                            teamlead_id=ids['teamlead_a'],
                            requested_usdt=Decimal('100'),
                            wallet_address=WALLET,
                            idempotency_key=f'parallel-{number}-{uuid.uuid4()}',
                            quote=_quote(rate='100', at=now),
                            now=now,
                        )
                        await db.commit()
                        return ('created', row.id)
                    except TeamLeadConflict:
                        await db.rollback()
                        return ('blocked', None)

            settlement_results = await asyncio.gather(
                request_settle(1),
                request_settle(2),
            )
            assert sorted(row[0] for row in settlement_results) == [
                'blocked',
                'created',
            ]
            settlement_id = next(
                row[1] for row in settlement_results if row[0] == 'created'
            )

            async def pay():
                async with AsyncSession(engine, expire_on_commit=False) as db:
                    stored = await db.get(Deposit, ids['deposit'])
                    local_actors = {'merchant': await db.get(Merchant, ids['merchant'])}
                    await _pay(db, local_actors, stored)
                    await db.commit()

            await asyncio.gather(pay(), pay())

            async def finalize(kind: str):
                async with AsyncSession(engine, expire_on_commit=False) as db:
                    try:
                        if kind == 'complete':
                            await complete_teamlead_settlement(
                                db,
                                settlement_id=settlement_id,
                                actor_id=ids['superadmin'],
                                tx_hash=f'tx-{uuid.uuid4()}',
                            )
                        else:
                            await reject_teamlead_settlement(
                                db,
                                settlement_id=settlement_id,
                                actor_id=ids['superadmin'],
                                reason='parallel opposite final',
                            )
                        await db.commit()
                        return 'done'
                    except TeamLeadConflict:
                        await db.rollback()
                        return 'conflict'

            final_results = await asyncio.gather(
                finalize('complete'),
                finalize('reject'),
            )
            assert sorted(final_results) == ['conflict', 'done']
            async with AsyncSession(engine) as verify:
                assert await verify.scalar(
                    select(func.count(TeamLeadSettlement.id)).where(
                        TeamLeadSettlement.teamlead_id == ids['teamlead_a'],
                        TeamLeadSettlement.status == 'pending',
                    )
                ) == 0
                assert await verify.scalar(
                    select(func.count(TeamLeadAccrual.id)).where(
                        TeamLeadAccrual.deposit_id == ids['deposit']
                    )
                ) == 1
                assert await verify.scalar(
                    select(func.count(TeamLeadMerchantAccrual.id)).where(
                        TeamLeadMerchantAccrual.deposit_id == ids['deposit']
                    )
                ) == 1
                assert await verify.scalar(
                    select(func.count(PlatformLedgerEntry.id)).where(
                        PlatformLedgerEntry.operation_id == ids['deposit'],
                        PlatformLedgerEntry.entry_type == 'teamlead_expense',
                    )
                ) == 2
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


def test_postgres_teamlead_web_is_2fa_csrf_isolated_and_admin_can_only_create_basic():
    async def scenario():
        engine = create_async_engine(_database_url())
        otp_secret = 'JBSWY3DPEHPK3PXP'
        password = 'teamlead-web-password'
        admin_password = 'admin-web-password'
        superadmin_password = 'superadmin-web-password'
        try:
            async with AsyncSession(engine, expire_on_commit=False) as setup:
                actors = await _actors(setup)
                actors['teamlead_a'].password_hash = hash_password(password)
                actors['teamlead_a'].twofa_enabled = True
                actors['teamlead_a'].twofa_secret = encrypt_secret(otp_secret)
                actors['admin'].password_hash = hash_password(admin_password)
                actors['admin'].twofa_secret = encrypt_secret(otp_secret)
                actors['superadmin'].password_hash = hash_password(
                    superadmin_password
                )
                actors['superadmin'].twofa_secret = encrypt_secret(otp_secret)
                await create_or_replace_assignment(
                    setup,
                    teamlead_id=actors['teamlead_a'].id,
                    trader_id=actors['trader_a'].id,
                    commission_percent=Decimal('0.500000'),
                    actor_id=actors['superadmin'].id,
                    reason='web isolation',
                    effective_from=datetime.now(timezone.utc) - timedelta(days=1),
                )
                other = await create_or_replace_assignment(
                    setup,
                    teamlead_id=actors['teamlead_b'].id,
                    trader_id=actors['trader_b'].id,
                    commission_percent=Decimal('0.500000'),
                    actor_id=actors['superadmin'].id,
                    reason='other TeamLead',
                    effective_from=datetime.now(timezone.utc) - timedelta(days=1),
                )
                setup.add(Requisite(
                    trader_id=actors['trader_a'].id,
                    owner_name='SENSITIVE OWNER NAME',
                    full_name='SENSITIVE FULL NAME',
                    method='c2c',
                    value_encrypted=encrypt_secret('4111111111111111'),
                    bank_name='Sensitive bank',
                    last4='1111',
                ))
                await adjust_teamlead_balance(
                    setup,
                    teamlead_id=actors['teamlead_a'].id,
                    actor_id=actors['superadmin'].id,
                    adjustment_type='available_credit',
                    amount_rub=Decimal('1000'),
                    idempotency_key=f'web-adjust-{uuid.uuid4()}',
                    reason='web processing test',
                )
                pending = await create_teamlead_settlement(
                    setup,
                    teamlead_id=actors['teamlead_a'].id,
                    requested_usdt=Decimal('10'),
                    wallet_address=WALLET,
                    idempotency_key=f'web-settle-{uuid.uuid4()}',
                    quote=_quote(rate='10'),
                )
                ids = {
                    'teamlead': actors['teamlead_a'].id,
                    'teamlead_email': actors['teamlead_a'].email,
                    'trader_email': actors['trader_a'].email,
                    'other_trader_email': actors['trader_b'].email,
                    'admin': actors['admin'].id,
                    'admin_hash': actors['admin'].password_hash,
                    'admin_email': actors['admin'].email,
                    'superadmin': actors['superadmin'].id,
                    'superadmin_hash': actors['superadmin'].password_hash,
                    'superadmin_email': actors['superadmin'].email,
                    'other_assignment': other.id,
                    'settlement': pending.id,
                }
                await setup.commit()

            admin_token = create_token(
                str(ids['admin']),
                'access',
                timedelta(minutes=5),
                {'auth': auth_state_marker(ids['admin_hash'])},
            )
            superadmin_token = create_token(
                str(ids['superadmin']),
                'access',
                timedelta(minutes=5),
                {'auth': auth_state_marker(ids['superadmin_hash'])},
            )
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport,
                base_url='https://localhost',
                follow_redirects=False,
            ) as client:
                created = await client.post(
                    '/api/v1/admin/users',
                    headers={'Authorization': f'Bearer {admin_token}'},
                    json={
                        'email': f'admin-created-{uuid.uuid4().hex}@example.test',
                        'password': 'created-teamlead-password',
                        'role': 'teamlead',
                    },
                )
                assert created.status_code == 200, created.text
                assert created.json()['role'] == 'teamlead'
                assert created.json()['twofa_setup_required'] is True
                created_by_superadmin = await client.post(
                    '/api/v1/admin/users',
                    headers={'Authorization': f'Bearer {superadmin_token}'},
                    json={
                        'email': f'super-created-{uuid.uuid4().hex}@example.test',
                        'password': 'created-teamlead-password',
                        'role': 'teamlead',
                    },
                )
                assert created_by_superadmin.status_code == 200
                assert created_by_superadmin.json()['role'] == 'teamlead'

                login_page = await client.get('/staff/login')
                login = await client.post(
                    '/staff/login',
                    data={
                        'csrf_token': _csrf_from_html(login_page),
                        'email': ids['teamlead_email'],
                        'password': password,
                        'otp': pyotp.TOTP(otp_secret).now(),
                    },
                )
                assert login.status_code == 303
                cabinet = await client.get('/staff/cabinet')
                assert cabinet.status_code == 200
                assert 'TeamLead кабинет' in cabinet.text
                assert ids['trader_email'] in cabinet.text
                assert ids['other_trader_email'] not in cabinet.text
                assert '4111111111111111' not in cabinet.text
                assert 'SENSITIVE FULL NAME' not in cabinet.text
                missing_csrf = await client.post(
                    '/staff/cabinet/teamlead/settlements/request',
                    data={
                        'requested_usdt': '1',
                        'wallet_address': WALLET,
                        'idempotency_key': f'csrf-{uuid.uuid4()}',
                    },
                )
                assert missing_csrf.status_code == 403
                unavailable = await client.post(
                    '/staff/cabinet/teamlead/settlements/request',
                    data={
                        'csrf_token': _csrf_from_html(cabinet),
                        'requested_usdt': '1',
                        'wallet_address': WALLET,
                        'idempotency_key': f'rapira-disabled-{uuid.uuid4()}',
                    },
                )
                assert unavailable.status_code == 303
                assert 'teamlead_rate_unavailable' in unavailable.headers['location']
                async with AsyncSession(engine) as unchanged:
                    assert await unchanged.scalar(
                        select(func.count(TeamLeadSettlement.id)).where(
                            TeamLeadSettlement.teamlead_id == ids['teamlead']
                        )
                    ) == 1
                    unchanged_balance = await unchanged.scalar(
                        select(TeamLeadBalance).where(
                            TeamLeadBalance.teamlead_id == ids['teamlead']
                        )
                    )
                    assert unchanged_balance.available_rub == Decimal('850.00')
                    assert unchanged_balance.frozen_rub == Decimal('150.00')

            async with httpx.AsyncClient(
                transport=transport,
                base_url='https://localhost',
                follow_redirects=False,
            ) as admin_client:
                login_page = await admin_client.get('/staff/login')
                login = await admin_client.post(
                    '/staff/login',
                    data={
                        'csrf_token': _csrf_from_html(login_page),
                        'email': ids['admin_email'],
                        'password': admin_password,
                        'otp': pyotp.TOTP(otp_secret).now(),
                    },
                )
                assert login.status_code == 303
                admin_cabinet = await admin_client.get('/staff/cabinet')
                assert admin_cabinet.status_code == 200
                staff_csrf = _csrf_from_html(admin_cabinet)
                denied_assignment = await admin_client.post(
                    '/staff/cabinet/teamlead/assignments',
                    data={
                        'csrf_token': staff_csrf,
                        'teamlead_id': str(ids['teamlead']),
                        'trader_id': str(actors['trader_a'].id),
                        'commission_percent': '99',
                        'reason': 'admin must be denied',
                    },
                )
                assert denied_assignment.status_code == 303
                denied_complete = await admin_client.post(
                    f"/staff/cabinet/teamlead/settlements/{ids['settlement']}/complete",
                    data={
                        'csrf_token': staff_csrf,
                        'tx_hash': 'admin-must-not-complete',
                    },
                )
                assert denied_complete.status_code == 303

            async with httpx.AsyncClient(
                transport=transport,
                base_url='https://localhost',
                follow_redirects=False,
            ) as super_client:
                login_page = await super_client.get('/staff/login')
                login = await super_client.post(
                    '/staff/login',
                    data={
                        'csrf_token': _csrf_from_html(login_page),
                        'email': ids['superadmin_email'],
                        'password': superadmin_password,
                        'otp': pyotp.TOTP(otp_secret).now(),
                    },
                )
                assert login.status_code == 303
                super_cabinet = await super_client.get('/staff/cabinet')
                assert super_cabinet.status_code == 200
                assert (
                    'TeamLead может превысить маржу сделки' in super_cabinet.text
                    or 'can exceed margin' in super_cabinet.text
                )
                completed = await super_client.post(
                    f"/staff/cabinet/teamlead/settlements/{ids['settlement']}/complete",
                    data={
                        'csrf_token': _csrf_from_html(super_cabinet),
                        'tx_hash': f'web-tx-{uuid.uuid4()}',
                    },
                )
                assert completed.status_code == 303

            async with AsyncSession(engine) as verify:
                assert await verify.scalar(
                    select(func.count(User.id)).where(
                        User.role == Role.teamlead.value
                    )
                ) >= 3
                settlement = await verify.get(
                    TeamLeadSettlement, ids['settlement']
                )
                assert settlement.status == 'completed'
                active = await verify.scalar(
                    select(TeamLeadTraderAssignment).where(
                        TeamLeadTraderAssignment.trader_id
                        == actors['trader_a'].id,
                        TeamLeadTraderAssignment.effective_to.is_(None),
                    )
                )
                assert active.commission_percent == Decimal('0.500000')
        finally:
            await application_engine.dispose()
            await engine.dispose()


def test_postgres_teamlead_2fa_onboarding_route_level_then_access_cabinet():
    async def scenario():
        engine = create_async_engine(_database_url())
        password = 'teamlead-onboard-password'
        try:
            async with AsyncSession(engine, expire_on_commit=False) as setup:
                actors = await _actors(setup)
                actors['teamlead_a'].password_hash = hash_password(password)
                actors['teamlead_a'].twofa_enabled = False
                actors['teamlead_a'].twofa_secret = None
                teamlead_email = actors['teamlead_a'].email
                teamlead_id = actors['teamlead_a'].id
                await setup.commit()
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport,
                base_url='https://localhost',
                follow_redirects=False,
            ) as client:
                login_page = await client.get('/staff/login')
                login = await client.post(
                    '/staff/login',
                    data={
                        'csrf_token': _csrf_from_html(login_page),
                        'email': teamlead_email,
                        'password': password,
                    },
                )
                assert login.status_code == 303
                first_cabinet = await client.get('/staff/cabinet')
                assert first_cabinet.status_code == 200
                prepare = await client.post(
                    '/staff/cabinet/security/2fa/prepare',
                    data={
                        'csrf_token': _csrf_from_html(first_cabinet),
                    },
                )
                assert prepare.status_code == 303
                async with AsyncSession(engine, expire_on_commit=False) as db:
                    teamlead = await db.get(User, teamlead_id)
                    otp_secret = decrypt_secret(teamlead.twofa_secret)
                enable = await client.post(
                    '/staff/cabinet/security/2fa/enable',
                    data={
                        'csrf_token': _csrf_from_html(first_cabinet),
                        'otp': pyotp.TOTP(otp_secret).now(),
                    },
                )
                assert enable.status_code == 303
                final_cabinet = await client.get('/staff/cabinet')
                assert final_cabinet.status_code == 200
                assert 'TeamLead' in final_cabinet.text
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_postgres_teamlead_merchant_assignment_history_and_created_at_attribution():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                actors = await _actors(db)
                base = datetime.now(timezone.utc) - timedelta(days=10)
                before, before_snapshot = await _deposit(
                    db,
                    actors,
                    created_at=base - timedelta(hours=1),
                )
                first = await create_or_replace_merchant_assignment(
                    db,
                    teamlead_id=actors['teamlead_a'].id,
                    merchant_id=actors['merchant'].id,
                    commission_percent=Decimal('1.250000'),
                    actor_id=actors['superadmin'].id,
                    reason='initial merchant referral',
                    valid_from=base,
                )
                assert await accrue_teamlead_merchant_commission(
                    db,
                    deposit=before,
                    snapshot=before_snapshot,
                    platform_income_rub=before_snapshot.platform_income_amount,
                ) is None
                await _pay(db, actors, before)
                assert await db.scalar(
                    select(func.count(TeamLeadMerchantAccrual.id)).where(
                        TeamLeadMerchantAccrual.deposit_id == before.id
                    )
                ) == 0

                during, _ = await _deposit(
                    db,
                    actors,
                    created_at=base + timedelta(hours=1),
                )
                resolved = await resolve_merchant_assignment_for_deposit(
                    db,
                    deposit=during,
                )
                assert resolved.id == first.id
                await _pay(db, actors, during)
                during_accrual = await db.scalar(
                    select(TeamLeadMerchantAccrual).where(
                        TeamLeadMerchantAccrual.deposit_id == during.id
                    )
                )
                assert during_accrual.commission_percent_snapshot == Decimal(
                    '1.250000'
                )

                second = await create_or_replace_merchant_assignment(
                    db,
                    teamlead_id=actors['teamlead_b'].id,
                    merchant_id=actors['merchant'].id,
                    commission_percent=Decimal('2.000000'),
                    actor_id=actors['superadmin'].id,
                    reason='merchant reassigned',
                    valid_from=base + timedelta(days=2),
                )
                after, _ = await _deposit(
                    db,
                    actors,
                    created_at=base + timedelta(days=3),
                )
                assert (
                    await resolve_merchant_assignment_for_deposit(
                        db,
                        deposit=after,
                    )
                ).id == second.id
                await _pay(db, actors, after)
                after_accrual = await db.scalar(
                    select(TeamLeadMerchantAccrual).where(
                        TeamLeadMerchantAccrual.deposit_id == after.id
                    )
                )
                assert after_accrual.teamlead_id == actors['teamlead_b'].id
                assert first.valid_to == second.valid_from
                assert await db.scalar(
                    select(func.count(TeamLeadMerchantAssignment.id)).where(
                        TeamLeadMerchantAssignment.merchant_id
                        == actors['merchant'].id,
                        TeamLeadMerchantAssignment.valid_to.is_(None),
                    )
                ) == 1

                await close_merchant_assignment(
                    db,
                    merchant_id=actors['merchant'].id,
                    actor_id=actors['superadmin'].id,
                    reason='merchant referral ended',
                    valid_to=base + timedelta(days=4),
                )
                after_close, _ = await _deposit(
                    db,
                    actors,
                    created_at=base + timedelta(days=5),
                )
                await _pay(db, actors, after_close)
                assert await db.scalar(
                    select(func.count(TeamLeadMerchantAccrual.id)).where(
                        TeamLeadMerchantAccrual.deposit_id == after_close.id
                    )
                ) == 0
                with pytest.raises(
                    TeamLeadConflict,
                    match='merchant_assignment_period_overlap',
                ):
                    await create_or_replace_merchant_assignment(
                        db,
                        teamlead_id=actors['teamlead_a'].id,
                        merchant_id=actors['merchant'].id,
                        commission_percent=Decimal('3.000000'),
                        actor_id=actors['superadmin'].id,
                        reason='must not overlap history',
                        valid_from=base + timedelta(days=1),
                    )
                await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_postgres_independent_referral_matrix_shared_balance_settlement_and_reversal():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                actors = await _actors(db)
                base = datetime.now(timezone.utc) - timedelta(days=10)
                trader_balance_before = Decimal(
                    actors['trader_a'].trader_balance
                )

                without_assignments, _ = await _deposit(
                    db,
                    actors,
                    created_at=base - timedelta(days=1),
                )
                await _pay(db, actors, without_assignments)
                assert await db.scalar(
                    select(func.count(TeamLeadAccrual.id)).where(
                        TeamLeadAccrual.deposit_id == without_assignments.id
                    )
                ) == 0
                assert await db.scalar(
                    select(func.count(TeamLeadMerchantAccrual.id)).where(
                        TeamLeadMerchantAccrual.deposit_id
                        == without_assignments.id
                    )
                ) == 0

                await create_or_replace_merchant_assignment(
                    db,
                    teamlead_id=actors['teamlead_b'].id,
                    merchant_id=actors['merchant'].id,
                    commission_percent=Decimal('1.000000'),
                    actor_id=actors['superadmin'].id,
                    reason='merchant-only interval',
                    valid_from=base,
                )
                merchant_only, _ = await _deposit(
                    db,
                    actors,
                    created_at=base + timedelta(days=1),
                )
                await _pay(db, actors, merchant_only)
                assert await db.scalar(
                    select(func.count(TeamLeadAccrual.id)).where(
                        TeamLeadAccrual.deposit_id == merchant_only.id
                    )
                ) == 0
                assert (await db.scalar(
                    select(TeamLeadMerchantAccrual).where(
                        TeamLeadMerchantAccrual.deposit_id == merchant_only.id
                    )
                )).accrual_rub == Decimal('1000.00')

                await create_or_replace_assignment(
                    db,
                    teamlead_id=actors['teamlead_a'].id,
                    trader_id=actors['trader_a'].id,
                    commission_percent=Decimal('0.500000'),
                    actor_id=actors['superadmin'].id,
                    reason='trader referral starts',
                    effective_from=base + timedelta(days=2),
                )
                different_teamleads, _ = await _deposit(
                    db,
                    actors,
                    created_at=base + timedelta(days=3),
                )
                await _pay(db, actors, different_teamleads)
                assert (await db.scalar(
                    select(TeamLeadAccrual).where(
                        TeamLeadAccrual.deposit_id == different_teamleads.id
                    )
                )).teamlead_id == actors['teamlead_a'].id
                assert (await db.scalar(
                    select(TeamLeadMerchantAccrual).where(
                        TeamLeadMerchantAccrual.deposit_id
                        == different_teamleads.id
                    )
                )).teamlead_id == actors['teamlead_b'].id

                await create_or_replace_merchant_assignment(
                    db,
                    teamlead_id=actors['teamlead_a'].id,
                    merchant_id=actors['merchant'].id,
                    commission_percent=Decimal('1.000000'),
                    actor_id=actors['superadmin'].id,
                    reason='same TeamLead for both sources',
                    valid_from=base + timedelta(days=4),
                )
                same_teamlead, _ = await _deposit(
                    db,
                    actors,
                    created_at=base + timedelta(days=5),
                )
                first_result = await _pay(db, actors, same_teamlead)
                repeated_result = await settle_deposit_credit(
                    db,
                    merchant_id=actors['merchant'].id,
                    method=same_teamlead.method,
                    amount=same_teamlead.amount,
                    operation_id=same_teamlead.id,
                    description='merchant referral idempotency repeat',
                )
                assert first_result['merchant_payable_amount'] == '90000.00'
                assert repeated_result['merchant_payable_amount'] == '90000.00'
                assert await db.scalar(
                    select(func.count(TeamLeadAccrual.id)).where(
                        TeamLeadAccrual.deposit_id == same_teamlead.id
                    )
                ) == 1
                assert await db.scalar(
                    select(func.count(TeamLeadMerchantAccrual.id)).where(
                        TeamLeadMerchantAccrual.deposit_id == same_teamlead.id
                    )
                ) == 1
                assert await db.scalar(
                    select(func.count(TeamLeadLedgerEntry.id)).where(
                        TeamLeadLedgerEntry.deposit_id == same_teamlead.id,
                        TeamLeadLedgerEntry.source_type == 'trader_referral',
                    )
                ) == 1
                assert await db.scalar(
                    select(func.count(TeamLeadLedgerEntry.id)).where(
                        TeamLeadLedgerEntry.deposit_id == same_teamlead.id,
                        TeamLeadLedgerEntry.source_type == 'merchant_referral',
                    )
                ) == 1

                balance_a = await db.scalar(
                    select(TeamLeadBalance).where(
                        TeamLeadBalance.teamlead_id == actors['teamlead_a'].id
                    )
                )
                balance_b = await db.scalar(
                    select(TeamLeadBalance).where(
                        TeamLeadBalance.teamlead_id == actors['teamlead_b'].id
                    )
                )
                assert balance_a.available_rub == Decimal('2000.00')
                assert balance_b.available_rub == Decimal('2000.00')

                settlement_at = datetime.now(timezone.utc)
                settlement = await create_teamlead_settlement(
                    db,
                    teamlead_id=actors['teamlead_a'].id,
                    requested_usdt=Decimal('15'),
                    wallet_address=WALLET,
                    idempotency_key=f'combined-sources-{uuid.uuid4()}',
                    quote=_quote(rate='100', at=settlement_at),
                    now=settlement_at,
                )
                await complete_teamlead_settlement(
                    db,
                    settlement_id=settlement.id,
                    actor_id=actors['superadmin'].id,
                    tx_hash=f'combined-sources-{uuid.uuid4()}',
                    now=settlement_at,
                )
                assert balance_a.available_rub == Decimal('0.00')
                assert balance_a.total_paid_rub == Decimal('1500.00')

                reversed_rows = await reverse_teamlead_accruals_for_deposit(
                    db,
                    deposit_id=same_teamlead.id,
                    actor_id=actors['superadmin'].id,
                    reason='reverse both independent referrals',
                )
                assert len(reversed_rows) == 2
                assert balance_a.debt_rub == Decimal('1500.00')
                future, _ = await _deposit(
                    db,
                    actors,
                    created_at=base + timedelta(days=6),
                )
                await _pay(db, actors, future)
                assert balance_a.debt_rub == Decimal('0.00')
                assert balance_a.available_rub == Decimal('0.00')
                assert balance_a.total_earned_rub == Decimal('2000.00')

                merchant_balance = await db.scalar(
                    select(Balance).where(
                        Balance.merchant_id == actors['merchant'].id
                    )
                )
                assert merchant_balance.available == Decimal('450000.00')
                assert actors['trader_a'].trader_balance == trader_balance_before
                assert await db.scalar(
                    select(func.count(MerchantRollingAllocation.id)).where(
                        MerchantRollingAllocation.merchant_id
                        == actors['merchant'].id
                    )
                ) == 0
                report_a = await reconcile_teamlead_account(
                    db,
                    actors['teamlead_a'].id,
                )
                report_b = await reconcile_teamlead_account(
                    db,
                    actors['teamlead_b'].id,
                )
                assert report_a['reconciled'], report_a
                assert report_b['reconciled'], report_b
                assert report_a['totals'][
                    'trader_referral_accrual_rub'
                ] == Decimal('1500.00')
                assert report_a['totals'][
                    'merchant_referral_accrual_rub'
                ] == Decimal('2000.00')
                income = await platform_income_dashboard(
                    db,
                    merchant_id=actors['merchant'].id,
                )
                assert income['totals']['reconciled'] is True
                assert income['totals']['expected_teamlead_expense'] == Decimal(
                    '5500.00'
                )
                assert income['totals'][
                    'expected_teamlead_expense_reversal'
                ] == Decimal('1500.00')
                await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())
