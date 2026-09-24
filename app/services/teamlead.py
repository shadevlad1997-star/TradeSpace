from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.tron import (
    TronAddressError,
    validate_trc20_address as validate_tron_address,
)
from app.core.enums import (
    Role,
    TeamLeadAccrualStatus,
    TeamLeadLedgerType,
    TeamLeadReferralSource,
    TeamLeadSettlementStatus,
)
from app.models import (
    Deposit,
    Merchant,
    OperationFeeSnapshot,
    TeamLeadAccrual,
    TeamLeadBalance,
    TeamLeadLedgerEntry,
    TeamLeadMerchantAccrual,
    TeamLeadMerchantAssignment,
    TeamLeadSettlement,
    TeamLeadTraderAssignment,
    User,
)
from app.services.ledger import (
    record_platform_teamlead_expense,
    record_platform_teamlead_expense_reversal,
)
from app.services.audit import audit as audit_event
from app.services.rapira import RollingRapiraQuote


logger = logging.getLogger('app.finance')
MONEY = Decimal('0.01')
PERCENT = Decimal('0.000001')
USDT = Decimal('0.000001')
TEAMLEAD_SETTLEMENT_FEE_USDT = Decimal('5.000000')
TEAMLEAD_SETTLEMENT_COOLDOWN = timedelta(hours=168)


class TeamLeadError(ValueError):
    code = 'teamlead_error'

    def __init__(self, message: str | None = None):
        super().__init__(message or self.code)


class TeamLeadPermissionDenied(TeamLeadError):
    code = 'teamlead_permission_denied'


class TeamLeadConflict(TeamLeadError):
    code = 'teamlead_conflict'


class TeamLeadInsufficientFunds(TeamLeadError):
    code = 'teamlead_insufficient_funds'


class TeamLeadDebtOutstanding(TeamLeadError):
    code = 'teamlead_debt_outstanding'


class TeamLeadSettlementUnavailable(TeamLeadError):
    code = 'teamlead_settlement_unavailable'


def _money(value: Any, *, allow_zero: bool = True) -> Decimal:
    try:
        result = Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise TeamLeadError('invalid_money') from exc
    if not result.is_finite() or result < 0 or (not allow_zero and result == 0):
        raise TeamLeadError('invalid_money')
    return result


def _signed_money(value: Any) -> Decimal:
    try:
        result = Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise TeamLeadError('invalid_money') from exc
    if not result.is_finite():
        raise TeamLeadError('invalid_money')
    return result


def _percent(value: Any) -> Decimal:
    try:
        result = Decimal(str(value)).quantize(PERCENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise TeamLeadError('invalid_commission_percent') from exc
    if not result.is_finite() or not Decimal('0') <= result <= Decimal('100'):
        raise TeamLeadError('invalid_commission_percent')
    return result


def _usdt(value: Any) -> Decimal:
    try:
        result = Decimal(str(value)).quantize(USDT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise TeamLeadError('invalid_requested_usdt') from exc
    if not result.is_finite() or result <= 0:
        raise TeamLeadError('invalid_requested_usdt')
    return result


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _ensure_balance_row(db: AsyncSession, teamlead_id: UUID) -> None:
    await db.execute(
        pg_insert(TeamLeadBalance)
        .values(
            id=uuid.uuid4(),
            teamlead_id=teamlead_id,
            available_rub=Decimal('0.00'),
            frozen_rub=Decimal('0.00'),
            debt_rub=Decimal('0.00'),
            total_earned_rub=Decimal('0.00'),
            total_paid_rub=Decimal('0.00'),
        )
        .on_conflict_do_nothing(index_elements=['teamlead_id'])
    )


async def get_teamlead_balance(
    db: AsyncSession,
    teamlead_id: UUID,
    *,
    lock: bool = False,
) -> TeamLeadBalance:
    await _ensure_balance_row(db, teamlead_id)
    statement = select(TeamLeadBalance).where(
        TeamLeadBalance.teamlead_id == teamlead_id
    )
    if lock:
        statement = statement.with_for_update()
    return (await db.execute(statement)).scalar_one()


async def create_or_replace_assignment(
    db: AsyncSession,
    *,
    teamlead_id: UUID,
    trader_id: UUID,
    commission_percent: Decimal,
    actor_id: UUID,
    reason: str,
    effective_from: datetime | None = None,
) -> TeamLeadTraderAssignment:
    reason = (reason or '').strip()
    if not reason:
        raise TeamLeadError('assignment_reason_required')
    effective_from = _utc(effective_from or datetime.now(timezone.utc))
    commission_percent = _percent(commission_percent)
    teamlead = (await db.execute(
        select(User).where(User.id == teamlead_id)
    )).scalar_one_or_none()
    trader = (await db.execute(
        select(User).where(User.id == trader_id).with_for_update()
    )).scalar_one_or_none()
    if (
        not teamlead
        or teamlead.role != Role.teamlead.value
        or not teamlead.is_active
        or teamlead.is_locked
    ):
        raise TeamLeadError('invalid_teamlead')
    if not trader or trader.role not in {Role.operator.value, Role.trader.value}:
        raise TeamLeadError('invalid_trader')

    current = (await db.execute(
        select(TeamLeadTraderAssignment)
        .where(
            TeamLeadTraderAssignment.trader_id == trader_id,
            TeamLeadTraderAssignment.effective_to.is_(None),
        )
        .with_for_update()
    )).scalar_one_or_none()
    if current:
        if effective_from <= _utc(current.effective_from):
            raise TeamLeadConflict('assignment_effective_from_conflict')
        current.effective_to = effective_from
        current.closed_by = actor_id
        current.close_reason = reason
    assignment = TeamLeadTraderAssignment(
        teamlead_id=teamlead_id,
        trader_id=trader_id,
        commission_percent=commission_percent,
        effective_from=effective_from,
        created_by=actor_id,
        creation_reason=reason,
    )
    db.add(assignment)
    await _ensure_balance_row(db, teamlead_id)
    await db.flush()
    return assignment


async def resolve_assignment_for_deposit(
    db: AsyncSession,
    *,
    deposit: Deposit,
    trader_id: UUID,
    lock: bool = False,
) -> TeamLeadTraderAssignment | None:
    created_at = _utc(deposit.created_at)
    statement = (
        select(TeamLeadTraderAssignment)
        .where(
            TeamLeadTraderAssignment.trader_id == trader_id,
            TeamLeadTraderAssignment.effective_from <= created_at,
            or_(
                TeamLeadTraderAssignment.effective_to.is_(None),
                TeamLeadTraderAssignment.effective_to > created_at,
            ),
        )
        .order_by(TeamLeadTraderAssignment.effective_from.desc())
        .limit(1)
    )
    if lock:
        statement = statement.with_for_update()
    return (await db.execute(statement)).scalar_one_or_none()


async def create_or_replace_merchant_assignment(
    db: AsyncSession,
    *,
    teamlead_id: UUID,
    merchant_id: UUID,
    commission_percent: Decimal,
    actor_id: UUID,
    reason: str,
    valid_from: datetime | None = None,
) -> TeamLeadMerchantAssignment:
    reason = (reason or '').strip()
    if not reason:
        raise TeamLeadError('merchant_assignment_reason_required')
    valid_from = _utc(valid_from or datetime.now(timezone.utc))
    commission_percent = _percent(commission_percent)
    teamlead = (await db.execute(
        select(User).where(User.id == teamlead_id)
    )).scalar_one_or_none()
    merchant = (await db.execute(
        select(Merchant).where(Merchant.id == merchant_id).with_for_update()
    )).scalar_one_or_none()
    if (
        not teamlead
        or teamlead.role != Role.teamlead.value
        or not teamlead.is_active
        or teamlead.is_locked
    ):
        raise TeamLeadError('invalid_teamlead')
    if merchant is None or merchant.is_archived:
        raise TeamLeadError('invalid_merchant')

    current = (await db.execute(
        select(TeamLeadMerchantAssignment)
        .where(
            TeamLeadMerchantAssignment.merchant_id == merchant_id,
            TeamLeadMerchantAssignment.valid_to.is_(None),
        )
        .with_for_update()
    )).scalar_one_or_none()
    current_id = None
    if current:
        if valid_from <= _utc(current.valid_from):
            raise TeamLeadConflict('merchant_assignment_valid_from_conflict')
        current.valid_to = valid_from
        current.closed_by = actor_id
        current.close_reason = reason
        current_id = current.id
        await db.flush()

    overlap = select(TeamLeadMerchantAssignment.id).where(
        TeamLeadMerchantAssignment.merchant_id == merchant_id,
        or_(
            TeamLeadMerchantAssignment.valid_to.is_(None),
            TeamLeadMerchantAssignment.valid_to > valid_from,
        ),
    )
    if current_id is not None:
        overlap = overlap.where(TeamLeadMerchantAssignment.id != current_id)
    if (await db.execute(overlap.limit(1))).scalar_one_or_none() is not None:
        raise TeamLeadConflict('merchant_assignment_period_overlap')

    assignment = TeamLeadMerchantAssignment(
        teamlead_id=teamlead_id,
        merchant_id=merchant_id,
        commission_percent=commission_percent,
        valid_from=valid_from,
        created_by=actor_id,
        reason=reason,
    )
    db.add(assignment)
    await _ensure_balance_row(db, teamlead_id)
    await db.flush()
    return assignment


async def close_merchant_assignment(
    db: AsyncSession,
    *,
    merchant_id: UUID,
    actor_id: UUID,
    reason: str,
    valid_to: datetime | None = None,
) -> TeamLeadMerchantAssignment:
    reason = (reason or '').strip()
    if not reason:
        raise TeamLeadError('merchant_assignment_close_reason_required')
    valid_to = _utc(valid_to or datetime.now(timezone.utc))
    merchant = (await db.execute(
        select(Merchant).where(Merchant.id == merchant_id).with_for_update()
    )).scalar_one_or_none()
    if merchant is None:
        raise TeamLeadError('invalid_merchant')
    current = (await db.execute(
        select(TeamLeadMerchantAssignment)
        .where(
            TeamLeadMerchantAssignment.merchant_id == merchant_id,
            TeamLeadMerchantAssignment.valid_to.is_(None),
        )
        .with_for_update()
    )).scalar_one_or_none()
    if current is None:
        raise TeamLeadError('merchant_assignment_not_found')
    if valid_to <= _utc(current.valid_from):
        raise TeamLeadConflict('merchant_assignment_valid_to_conflict')
    current.valid_to = valid_to
    current.closed_by = actor_id
    current.close_reason = reason
    await db.flush()
    return current


async def resolve_merchant_assignment_for_deposit(
    db: AsyncSession,
    *,
    deposit: Deposit,
    lock: bool = False,
) -> TeamLeadMerchantAssignment | None:
    created_at = _utc(deposit.created_at)
    statement = (
        select(TeamLeadMerchantAssignment)
        .where(
            TeamLeadMerchantAssignment.merchant_id == deposit.merchant_id,
            TeamLeadMerchantAssignment.valid_from <= created_at,
            or_(
                TeamLeadMerchantAssignment.valid_to.is_(None),
                TeamLeadMerchantAssignment.valid_to > created_at,
            ),
        )
        .order_by(TeamLeadMerchantAssignment.valid_from.desc())
        .limit(1)
    )
    if lock:
        statement = statement.with_for_update()
    return (await db.execute(statement)).scalar_one_or_none()


def _ledger(
    *,
    balance: TeamLeadBalance,
    teamlead_id: UUID,
    entry_type: str,
    amount_rub: Decimal,
    idempotency_key: str,
    reason: str,
    accrual_id: UUID | None = None,
    merchant_accrual_id: UUID | None = None,
    settlement_id: UUID | None = None,
    deposit_id: UUID | None = None,
    merchant_id: UUID | None = None,
    source_type: str | None = None,
    actor_id: UUID | None = None,
) -> TeamLeadLedgerEntry:
    return TeamLeadLedgerEntry(
        teamlead_id=teamlead_id,
        accrual_id=accrual_id,
        merchant_accrual_id=merchant_accrual_id,
        settlement_id=settlement_id,
        deposit_id=deposit_id,
        merchant_id=merchant_id,
        actor_id=actor_id,
        entry_type=entry_type,
        source_type=source_type,
        amount_rub=_money(amount_rub),
        available_after=_money(balance.available_rub),
        frozen_after=_money(balance.frozen_rub),
        debt_after=_money(balance.debt_rub),
        idempotency_key=idempotency_key,
        reason=reason,
    )


async def accrue_teamlead_commission(
    db: AsyncSession,
    *,
    deposit: Deposit,
    snapshot: OperationFeeSnapshot,
    platform_income_rub: Decimal,
) -> TeamLeadAccrual | None:
    if deposit.status != 'paid':
        return None
    if snapshot.executor_type != 'trader':
        return None
    assignment = await resolve_assignment_for_deposit(
        db,
        deposit=deposit,
        trader_id=snapshot.executor_id,
        lock=True,
    )
    if assignment is None:
        return None
    balance = await get_teamlead_balance(db, assignment.teamlead_id, lock=True)
    existing = (await db.execute(
        select(TeamLeadAccrual)
        .where(TeamLeadAccrual.deposit_id == deposit.id)
        .with_for_update()
    )).scalar_one_or_none()
    if existing:
        return existing

    gross = _money(snapshot.calculation_base_amount)
    if gross != _money(deposit.amount):
        raise TeamLeadError('teamlead_gross_snapshot_mismatch')
    accrual_amount = _money(gross * _percent(assignment.commission_percent) / Decimal('100'))
    debt_offset = min(_money(balance.debt_rub), accrual_amount)
    available_credit = _money(accrual_amount - debt_offset)
    balance.debt_rub = _money(balance.debt_rub - debt_offset)
    balance.available_rub = _money(balance.available_rub + available_credit)
    balance.total_earned_rub = _money(balance.total_earned_rub + accrual_amount)

    accrual = TeamLeadAccrual(
        teamlead_id=assignment.teamlead_id,
        trader_id=snapshot.executor_id,
        assignment_id=assignment.id,
        deposit_id=deposit.id,
        gross_rub=gross,
        commission_percent_snapshot=_percent(assignment.commission_percent),
        accrual_rub=accrual_amount,
        credited_to_available_rub=available_credit,
        applied_to_debt_rub=debt_offset,
        status=TeamLeadAccrualStatus.credited.value,
    )
    db.add(accrual)
    await db.flush()
    if debt_offset:
        db.add(_ledger(
            balance=balance,
            teamlead_id=assignment.teamlead_id,
            accrual_id=accrual.id,
            deposit_id=deposit.id,
            merchant_id=deposit.merchant_id,
            source_type=TeamLeadReferralSource.trader_referral.value,
            entry_type=TeamLeadLedgerType.debt_offset.value,
            amount_rub=debt_offset,
            idempotency_key=f'teamlead:{deposit.id}:debt-offset',
            reason='TeamLead accrual applied to outstanding debt',
        ))
    if available_credit:
        db.add(_ledger(
            balance=balance,
            teamlead_id=assignment.teamlead_id,
            accrual_id=accrual.id,
            deposit_id=deposit.id,
            merchant_id=deposit.merchant_id,
            source_type=TeamLeadReferralSource.trader_referral.value,
            entry_type=TeamLeadLedgerType.accrual_credit.value,
            amount_rub=available_credit,
            idempotency_key=f'teamlead:{deposit.id}:accrual-credit',
            reason='TeamLead commission from paid deposit gross',
        ))
    await record_platform_teamlead_expense(
        db,
        deposit.merchant_id,
        accrual_amount,
        deposit.id,
        f'teamlead:{deposit.id}:platform-expense',
        'TeamLead commission expense',
    )
    if accrual_amount > _money(platform_income_rub):
        await audit_event(
            db,
            'teamlead_loss_making_operation',
            'deposit',
            None,
            deposit.id,
            None,
            {
                'deposit_id': str(deposit.id),
                'teamlead_id': str(assignment.teamlead_id),
                'platform_income_rub': str(_money(platform_income_rub)),
                'teamlead_expense_rub': str(accrual_amount),
            },
        )
        logger.warning(
            'teamlead_loss_making_operation',
            extra={
                'deposit_id': str(deposit.id),
                'teamlead_id': str(assignment.teamlead_id),
                'platform_income_rub': str(_money(platform_income_rub)),
                'teamlead_expense_rub': str(accrual_amount),
            },
        )
    return accrual


async def accrue_teamlead_merchant_commission(
    db: AsyncSession,
    *,
    deposit: Deposit,
    snapshot: OperationFeeSnapshot,
    platform_income_rub: Decimal,
) -> TeamLeadMerchantAccrual | None:
    if deposit.status != 'paid':
        return None
    assignment = await resolve_merchant_assignment_for_deposit(
        db,
        deposit=deposit,
        lock=True,
    )
    if assignment is None:
        return None
    balance = await get_teamlead_balance(db, assignment.teamlead_id, lock=True)
    existing = (await db.execute(
        select(TeamLeadMerchantAccrual)
        .where(
            TeamLeadMerchantAccrual.deposit_id == deposit.id,
            TeamLeadMerchantAccrual.source_type
            == TeamLeadReferralSource.merchant_referral.value,
        )
        .with_for_update()
    )).scalar_one_or_none()
    if existing:
        return existing

    gross = _money(snapshot.calculation_base_amount)
    if gross != _money(deposit.amount):
        raise TeamLeadError('teamlead_merchant_gross_snapshot_mismatch')
    accrual_amount = _money(
        gross * _percent(assignment.commission_percent) / Decimal('100')
    )
    debt_offset = min(_money(balance.debt_rub), accrual_amount)
    available_credit = _money(accrual_amount - debt_offset)
    balance.debt_rub = _money(balance.debt_rub - debt_offset)
    balance.available_rub = _money(balance.available_rub + available_credit)
    balance.total_earned_rub = _money(
        balance.total_earned_rub + accrual_amount
    )

    accrual = TeamLeadMerchantAccrual(
        teamlead_id=assignment.teamlead_id,
        merchant_id=deposit.merchant_id,
        assignment_id=assignment.id,
        deposit_id=deposit.id,
        source_type=TeamLeadReferralSource.merchant_referral.value,
        gross_rub=gross,
        commission_percent_snapshot=_percent(assignment.commission_percent),
        accrual_rub=accrual_amount,
        credited_to_available_rub=available_credit,
        applied_to_debt_rub=debt_offset,
        status=TeamLeadAccrualStatus.credited.value,
    )
    db.add(accrual)
    await db.flush()
    if debt_offset:
        db.add(_ledger(
            balance=balance,
            teamlead_id=assignment.teamlead_id,
            merchant_accrual_id=accrual.id,
            deposit_id=deposit.id,
            merchant_id=deposit.merchant_id,
            source_type=TeamLeadReferralSource.merchant_referral.value,
            entry_type=TeamLeadLedgerType.debt_offset.value,
            amount_rub=debt_offset,
            idempotency_key=(
                f'teamlead:{deposit.id}:merchant-referral:debt-offset'
            ),
            reason='Merchant referral accrual applied to outstanding debt',
        ))
    if available_credit:
        db.add(_ledger(
            balance=balance,
            teamlead_id=assignment.teamlead_id,
            merchant_accrual_id=accrual.id,
            deposit_id=deposit.id,
            merchant_id=deposit.merchant_id,
            source_type=TeamLeadReferralSource.merchant_referral.value,
            entry_type=TeamLeadLedgerType.accrual_credit.value,
            amount_rub=available_credit,
            idempotency_key=(
                f'teamlead:{deposit.id}:merchant-referral:accrual-credit'
            ),
            reason='TeamLead merchant referral commission from paid gross',
        ))
    await record_platform_teamlead_expense(
        db,
        deposit.merchant_id,
        accrual_amount,
        deposit.id,
        f'teamlead:{deposit.id}:merchant-referral:platform-expense',
        'TeamLead merchant referral commission expense',
    )
    if accrual_amount > _money(platform_income_rub):
        await audit_event(
            db,
            'teamlead_merchant_referral_loss_making_operation',
            'deposit',
            None,
            deposit.id,
            None,
            {
                'deposit_id': str(deposit.id),
                'teamlead_id': str(assignment.teamlead_id),
                'merchant_id': str(deposit.merchant_id),
                'platform_income_rub': str(_money(platform_income_rub)),
                'teamlead_expense_rub': str(accrual_amount),
                'source_type': TeamLeadReferralSource.merchant_referral.value,
            },
        )
        logger.warning(
            'teamlead_merchant_referral_loss_making_operation',
            extra={
                'deposit_id': str(deposit.id),
                'teamlead_id': str(assignment.teamlead_id),
                'merchant_id': str(deposit.merchant_id),
                'platform_income_rub': str(_money(platform_income_rub)),
                'teamlead_expense_rub': str(accrual_amount),
                'source_type': TeamLeadReferralSource.merchant_referral.value,
            },
        )
    trader_accrual_amount = (await db.execute(
        select(TeamLeadAccrual.accrual_rub).where(
            TeamLeadAccrual.deposit_id == deposit.id
        )
    )).scalar_one_or_none()
    combined_expense = _money(
        accrual_amount + _money(trader_accrual_amount or Decimal('0.00'))
    )
    if trader_accrual_amount is not None and combined_expense > _money(
        platform_income_rub
    ):
        await audit_event(
            db,
            'teamlead_combined_referral_loss_making_operation',
            'deposit',
            None,
            deposit.id,
            None,
            {
                'deposit_id': str(deposit.id),
                'platform_income_rub': str(_money(platform_income_rub)),
                'teamlead_expense_rub': str(combined_expense),
                'source_types': [
                    TeamLeadReferralSource.trader_referral.value,
                    TeamLeadReferralSource.merchant_referral.value,
                ],
            },
        )
        logger.warning(
            'teamlead_combined_referral_loss_making_operation',
            extra={
                'deposit_id': str(deposit.id),
                'platform_income_rub': str(_money(platform_income_rub)),
                'teamlead_expense_rub': str(combined_expense),
            },
        )
    return accrual


async def reverse_teamlead_accrual(
    db: AsyncSession,
    *,
    deposit_id: UUID,
    actor_id: UUID,
    reason: str,
    now: datetime | None = None,
) -> TeamLeadAccrual | None:
    reason = (reason or '').strip()
    if not reason:
        raise TeamLeadError('reversal_reason_required')
    now = _utc(now or datetime.now(timezone.utc))
    deposit = (await db.execute(
        select(Deposit).where(Deposit.id == deposit_id).with_for_update()
    )).scalar_one_or_none()
    if deposit is None:
        raise TeamLeadError('deposit_not_found')
    accrual_hint = (await db.execute(
        select(TeamLeadAccrual).where(TeamLeadAccrual.deposit_id == deposit_id)
    )).scalar_one_or_none()
    if accrual_hint is None:
        return None
    assignment = (await db.execute(
        select(TeamLeadTraderAssignment)
        .where(TeamLeadTraderAssignment.id == accrual_hint.assignment_id)
        .with_for_update()
    )).scalar_one()
    balance = await get_teamlead_balance(db, assignment.teamlead_id, lock=True)
    accrual = (await db.execute(
        select(TeamLeadAccrual)
        .where(TeamLeadAccrual.id == accrual_hint.id)
        .with_for_update()
    )).scalar_one()
    if accrual.status == TeamLeadAccrualStatus.reversed.value:
        return accrual

    reversal = _money(accrual.accrual_rub)
    from_available = min(_money(balance.available_rub), reversal)
    balance.available_rub = _money(balance.available_rub - from_available)
    balance.debt_rub = _money(balance.debt_rub + reversal - from_available)
    balance.total_earned_rub = _money(balance.total_earned_rub - reversal)
    accrual.status = TeamLeadAccrualStatus.reversed.value
    accrual.reversed_at = now
    accrual.reversal_reason = reason
    db.add(_ledger(
        balance=balance,
        teamlead_id=accrual.teamlead_id,
        accrual_id=accrual.id,
        deposit_id=deposit.id,
        merchant_id=deposit.merchant_id,
        source_type=TeamLeadReferralSource.trader_referral.value,
        actor_id=actor_id,
        entry_type=TeamLeadLedgerType.accrual_reversal.value,
        amount_rub=reversal,
        idempotency_key=f'teamlead:{deposit.id}:accrual-reversal',
        reason=reason,
    ))
    await record_platform_teamlead_expense_reversal(
        db,
        deposit.merchant_id,
        reversal,
        deposit.id,
        f'teamlead:{deposit.id}:platform-expense-reversal',
        reason,
    )
    return accrual


async def reverse_teamlead_merchant_accrual(
    db: AsyncSession,
    *,
    deposit_id: UUID,
    actor_id: UUID,
    reason: str,
    now: datetime | None = None,
) -> TeamLeadMerchantAccrual | None:
    reason = (reason or '').strip()
    if not reason:
        raise TeamLeadError('reversal_reason_required')
    now = _utc(now or datetime.now(timezone.utc))
    deposit = (await db.execute(
        select(Deposit).where(Deposit.id == deposit_id).with_for_update()
    )).scalar_one_or_none()
    if deposit is None:
        raise TeamLeadError('deposit_not_found')
    accrual_hint = (await db.execute(
        select(TeamLeadMerchantAccrual).where(
            TeamLeadMerchantAccrual.deposit_id == deposit_id,
            TeamLeadMerchantAccrual.source_type
            == TeamLeadReferralSource.merchant_referral.value,
        )
    )).scalar_one_or_none()
    if accrual_hint is None:
        return None
    balance = await get_teamlead_balance(
        db,
        accrual_hint.teamlead_id,
        lock=True,
    )
    accrual = (await db.execute(
        select(TeamLeadMerchantAccrual)
        .where(TeamLeadMerchantAccrual.id == accrual_hint.id)
        .with_for_update()
    )).scalar_one()
    if accrual.status == TeamLeadAccrualStatus.reversed.value:
        return accrual

    reversal = _money(accrual.accrual_rub)
    from_available = min(_money(balance.available_rub), reversal)
    balance.available_rub = _money(balance.available_rub - from_available)
    balance.debt_rub = _money(balance.debt_rub + reversal - from_available)
    balance.total_earned_rub = _money(balance.total_earned_rub - reversal)
    accrual.status = TeamLeadAccrualStatus.reversed.value
    accrual.reversed_at = now
    accrual.reversal_reason = reason
    db.add(_ledger(
        balance=balance,
        teamlead_id=accrual.teamlead_id,
        merchant_accrual_id=accrual.id,
        deposit_id=deposit.id,
        merchant_id=deposit.merchant_id,
        source_type=TeamLeadReferralSource.merchant_referral.value,
        actor_id=actor_id,
        entry_type=TeamLeadLedgerType.accrual_reversal.value,
        amount_rub=reversal,
        idempotency_key=(
            f'teamlead:{deposit.id}:merchant-referral:accrual-reversal'
        ),
        reason=reason,
    ))
    await record_platform_teamlead_expense_reversal(
        db,
        deposit.merchant_id,
        reversal,
        deposit.id,
        f'teamlead:{deposit.id}:merchant-referral:platform-expense-reversal',
        reason,
    )
    return accrual


async def reverse_teamlead_accruals_for_deposit(
    db: AsyncSession,
    *,
    deposit_id: UUID,
    actor_id: UUID,
    reason: str,
    now: datetime | None = None,
) -> list[TeamLeadAccrual | TeamLeadMerchantAccrual]:
    reversed_rows: list[TeamLeadAccrual | TeamLeadMerchantAccrual] = []
    trader_accrual = await reverse_teamlead_accrual(
        db,
        deposit_id=deposit_id,
        actor_id=actor_id,
        reason=reason,
        now=now,
    )
    if trader_accrual is not None:
        reversed_rows.append(trader_accrual)
    merchant_accrual = await reverse_teamlead_merchant_accrual(
        db,
        deposit_id=deposit_id,
        actor_id=actor_id,
        reason=reason,
        now=now,
    )
    if merchant_accrual is not None:
        reversed_rows.append(merchant_accrual)
    return reversed_rows


def _validate_live_quote(
    quote: RollingRapiraQuote,
    *,
    now: datetime,
) -> Decimal:
    if (
        quote.symbol != 'USDT/RUB'
        or quote.side != 'ask'
        or quote.source != 'rapira_live'
        or quote.provider_field != 'askPrice'
        or quote.stale
        or quote.freshness_basis not in {'provider_timestamp', 'fetched_at'}
    ):
        raise TeamLeadSettlementUnavailable('teamlead_rate_unavailable')
    try:
        rate = Decimal(str(quote.rate))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise TeamLeadSettlementUnavailable('teamlead_rate_unavailable') from exc
    if not rate.is_finite() or rate <= 0:
        raise TeamLeadSettlementUnavailable('teamlead_rate_unavailable')
    fetched_at = _utc(quote.fetched_at)
    max_age = timedelta(seconds=settings.ROLLING_RAPIRA_MAX_AGE_SECONDS)
    age = now - fetched_at
    if age < timedelta(seconds=-5) or age > max_age:
        raise TeamLeadSettlementUnavailable('teamlead_rate_unavailable')
    if quote.freshness_basis == 'provider_timestamp':
        if quote.provider_timestamp is None:
            raise TeamLeadSettlementUnavailable('teamlead_rate_unavailable')
        provider_age = fetched_at - _utc(quote.provider_timestamp)
        if provider_age < timedelta(seconds=-5) or provider_age > max_age:
            raise TeamLeadSettlementUnavailable('teamlead_rate_unavailable')
    elif quote.provider_timestamp is not None:
        raise TeamLeadSettlementUnavailable('teamlead_rate_unavailable')
    return rate


def validate_trc20_address(wallet_address: str) -> str:
    try:
        return validate_tron_address(wallet_address)
    except TronAddressError as exc:
        raise TeamLeadError(exc.code) from exc


async def create_teamlead_settlement(
    db: AsyncSession,
    *,
    teamlead_id: UUID,
    requested_usdt: Decimal,
    wallet_address: str,
    idempotency_key: str,
    quote: RollingRapiraQuote,
    now: datetime | None = None,
) -> TeamLeadSettlement:
    # The caller must obtain quote over HTTP before entering this function.
    now = _utc(now or datetime.now(timezone.utc))
    requested_usdt = _usdt(requested_usdt)
    wallet_address = validate_trc20_address(wallet_address)
    idempotency_key = (idempotency_key or '').strip()
    if not idempotency_key:
        raise TeamLeadError('idempotency_key_required')
    rate = _validate_live_quote(quote, now=now)
    total_usdt = (requested_usdt + TEAMLEAD_SETTLEMENT_FEE_USDT).quantize(USDT)
    requested_rub = _money(requested_usdt * rate)
    fee_rub = _money(TEAMLEAD_SETTLEMENT_FEE_USDT * rate)
    total_rub = _money(requested_rub + fee_rub)

    teamlead = (await db.execute(
        select(User).where(User.id == teamlead_id)
    )).scalar_one_or_none()
    if (
        teamlead is None
        or teamlead.role != Role.teamlead.value
        or not teamlead.is_active
        or teamlead.is_locked
    ):
        raise TeamLeadPermissionDenied('invalid_teamlead')
    balance = await get_teamlead_balance(db, teamlead_id, lock=True)
    existing = (await db.execute(
        select(TeamLeadSettlement)
        .where(TeamLeadSettlement.idempotency_key == idempotency_key)
        .with_for_update()
    )).scalar_one_or_none()
    if existing:
        if (
            existing.teamlead_id != teamlead_id
            or _usdt(existing.requested_usdt) != requested_usdt
            or existing.wallet_address != wallet_address
        ):
            raise TeamLeadConflict('settlement_idempotency_mismatch')
        return existing
    if _money(balance.debt_rub) > 0:
        raise TeamLeadDebtOutstanding()
    pending = (await db.execute(
        select(TeamLeadSettlement.id).where(
            TeamLeadSettlement.teamlead_id == teamlead_id,
            TeamLeadSettlement.status == TeamLeadSettlementStatus.pending.value,
        )
    )).scalar_one_or_none()
    if pending:
        raise TeamLeadConflict('teamlead_settlement_pending')
    last_completed_at = (await db.execute(
        select(func.max(TeamLeadSettlement.completed_at)).where(
            TeamLeadSettlement.teamlead_id == teamlead_id,
            TeamLeadSettlement.status == TeamLeadSettlementStatus.completed.value,
        )
    )).scalar_one()
    if last_completed_at is not None:
        next_at = _utc(last_completed_at) + TEAMLEAD_SETTLEMENT_COOLDOWN
        if now < next_at:
            raise TeamLeadConflict(
                f'teamlead_settlement_cooldown_until:{next_at.isoformat()}'
            )
    if _money(balance.available_rub) < total_rub:
        raise TeamLeadInsufficientFunds()

    settlement = TeamLeadSettlement(
        teamlead_id=teamlead_id,
        requested_usdt=requested_usdt,
        fee_usdt=TEAMLEAD_SETTLEMENT_FEE_USDT,
        total_debit_usdt=total_usdt,
        rapira_rate_rub=rate,
        rate_symbol=quote.symbol,
        rate_source=quote.source,
        rate_side=quote.side,
        provider_timestamp=quote.provider_timestamp,
        fetched_at=_utc(quote.fetched_at),
        freshness_basis=quote.freshness_basis,
        requested_rub=requested_rub,
        fee_rub=fee_rub,
        total_debit_rub=total_rub,
        network='TRC20',
        wallet_address=wallet_address,
        status=TeamLeadSettlementStatus.pending.value,
        idempotency_key=idempotency_key,
        requested_at=now,
    )
    db.add(settlement)
    balance.available_rub = _money(balance.available_rub - total_rub)
    balance.frozen_rub = _money(balance.frozen_rub + total_rub)
    await db.flush()
    db.add(_ledger(
        balance=balance,
        teamlead_id=teamlead_id,
        settlement_id=settlement.id,
        entry_type=TeamLeadLedgerType.settlement_freeze.value,
        amount_rub=total_rub,
        idempotency_key=f'teamlead:settlement:{settlement.id}:freeze',
        reason='TeamLead settlement requested',
    ))
    return settlement


async def _locked_settlement_and_balance(
    db: AsyncSession,
    settlement_id: UUID,
) -> tuple[TeamLeadSettlement, TeamLeadBalance]:
    hint = (await db.execute(
        select(TeamLeadSettlement.teamlead_id).where(
            TeamLeadSettlement.id == settlement_id
        )
    )).scalar_one_or_none()
    if hint is None:
        raise TeamLeadError('settlement_not_found')
    balance = await get_teamlead_balance(db, hint, lock=True)
    settlement = (await db.execute(
        select(TeamLeadSettlement)
        .where(TeamLeadSettlement.id == settlement_id)
        .with_for_update()
    )).scalar_one()
    if settlement.teamlead_id != hint:
        raise TeamLeadConflict('settlement_owner_changed')
    return settlement, balance


async def reject_teamlead_settlement(
    db: AsyncSession,
    *,
    settlement_id: UUID,
    actor_id: UUID,
    reason: str,
    now: datetime | None = None,
) -> TeamLeadSettlement:
    reason = (reason or '').strip()
    if not reason:
        raise TeamLeadError('reject_reason_required')
    settlement, balance = await _locked_settlement_and_balance(db, settlement_id)
    if settlement.status == TeamLeadSettlementStatus.rejected.value:
        return settlement
    if settlement.status != TeamLeadSettlementStatus.pending.value:
        raise TeamLeadConflict('settlement_already_completed')
    amount = _money(settlement.total_debit_rub)
    if _money(balance.frozen_rub) < amount:
        raise TeamLeadConflict('invalid_teamlead_frozen_balance')
    balance.frozen_rub = _money(balance.frozen_rub - amount)
    balance.available_rub = _money(balance.available_rub + amount)
    settlement.status = TeamLeadSettlementStatus.rejected.value
    settlement.rejected_at = _utc(now or datetime.now(timezone.utc))
    settlement.processed_by = actor_id
    settlement.reject_reason = reason
    db.add(_ledger(
        balance=balance,
        teamlead_id=settlement.teamlead_id,
        settlement_id=settlement.id,
        actor_id=actor_id,
        entry_type=TeamLeadLedgerType.settlement_release.value,
        amount_rub=amount,
        idempotency_key=f'teamlead:settlement:{settlement.id}:release',
        reason=reason,
    ))
    return settlement


async def complete_teamlead_settlement(
    db: AsyncSession,
    *,
    settlement_id: UUID,
    actor_id: UUID,
    tx_hash: str,
    now: datetime | None = None,
) -> TeamLeadSettlement:
    tx_hash = (tx_hash or '').strip()
    if not tx_hash:
        raise TeamLeadError('tx_hash_required')
    settlement, balance = await _locked_settlement_and_balance(db, settlement_id)
    if settlement.status == TeamLeadSettlementStatus.completed.value:
        return settlement
    if settlement.status != TeamLeadSettlementStatus.pending.value:
        raise TeamLeadConflict('settlement_already_rejected')
    amount = _money(settlement.total_debit_rub)
    existing_duplicate = (await db.execute(
        select(TeamLeadSettlement).where(
            TeamLeadSettlement.status == TeamLeadSettlementStatus.completed.value,
            TeamLeadSettlement.network == settlement.network,
            TeamLeadSettlement.tx_hash == tx_hash,
            TeamLeadSettlement.id != settlement.id,
        )
    )).scalar_one_or_none()
    if existing_duplicate:
        raise TeamLeadConflict('teamlead_settlement_tx_hash_duplicate')
    if _money(balance.frozen_rub) < amount:
        raise TeamLeadConflict('invalid_teamlead_frozen_balance')
    balance.frozen_rub = _money(balance.frozen_rub - amount)
    amount_paid = _money(settlement.requested_rub)
    balance.total_paid_rub = _money(balance.total_paid_rub + amount_paid)
    settlement.status = TeamLeadSettlementStatus.completed.value
    settlement.completed_at = _utc(now or datetime.now(timezone.utc))
    settlement.processed_by = actor_id
    settlement.tx_hash = tx_hash
    db.add(_ledger(
        balance=balance,
        teamlead_id=settlement.teamlead_id,
        settlement_id=settlement.id,
        actor_id=actor_id,
        entry_type=TeamLeadLedgerType.settlement_complete.value,
        amount_rub=amount_paid,
        idempotency_key=f'teamlead:settlement:{settlement.id}:complete',
        reason=f'TRC20 settlement completed; tx={tx_hash}',
    ))
    return settlement


async def adjust_teamlead_balance(
    db: AsyncSession,
    *,
    teamlead_id: UUID,
    actor_id: UUID,
    adjustment_type: str,
    amount_rub: Decimal,
    idempotency_key: str,
    reason: str,
) -> TeamLeadLedgerEntry:
    reason = (reason or '').strip()
    idempotency_key = (idempotency_key or '').strip()
    amount = _money(amount_rub, allow_zero=False)
    if not reason:
        raise TeamLeadError('adjustment_reason_required')
    if not idempotency_key:
        raise TeamLeadError('idempotency_key_required')
    teamlead = (await db.execute(
        select(User).where(User.id == teamlead_id)
    )).scalar_one_or_none()
    if teamlead is None or teamlead.role != Role.teamlead.value:
        raise TeamLeadError('invalid_teamlead')
    balance = await get_teamlead_balance(db, teamlead_id, lock=True)
    existing = (await db.execute(
        select(TeamLeadLedgerEntry)
        .where(TeamLeadLedgerEntry.idempotency_key == idempotency_key)
        .with_for_update()
    )).scalar_one_or_none()
    if existing:
        if existing.teamlead_id != teamlead_id or _money(existing.amount_rub) != amount:
            raise TeamLeadConflict('adjustment_idempotency_mismatch')
        return existing
    entry_type = TeamLeadLedgerType.manual_adjustment.value
    if adjustment_type == 'available_credit':
        balance.available_rub = _money(balance.available_rub + amount)
    elif adjustment_type == 'available_debit':
        if _money(balance.available_rub) < amount:
            raise TeamLeadInsufficientFunds()
        balance.available_rub = _money(balance.available_rub - amount)
    elif adjustment_type == 'debt_increase':
        balance.debt_rub = _money(balance.debt_rub + amount)
    elif adjustment_type == 'debt_write_off':
        if _money(balance.debt_rub) < amount:
            raise TeamLeadConflict('write_off_exceeds_debt')
        balance.debt_rub = _money(balance.debt_rub - amount)
        entry_type = TeamLeadLedgerType.write_off.value
    else:
        raise TeamLeadError('invalid_adjustment_type')
    entry = _ledger(
        balance=balance,
        teamlead_id=teamlead_id,
        actor_id=actor_id,
        entry_type=entry_type,
        amount_rub=amount,
        idempotency_key=idempotency_key,
        reason=f'{adjustment_type}: {reason}',
    )
    db.add(entry)
    return entry


async def reconcile_teamlead_account(
    db: AsyncSession,
    teamlead_id: UUID,
) -> dict[str, Any]:
    balance = await get_teamlead_balance(db, teamlead_id)
    accrual_rows = (await db.execute(
        select(TeamLeadAccrual).where(TeamLeadAccrual.teamlead_id == teamlead_id)
    )).scalars().all()
    merchant_accrual_rows = (await db.execute(
        select(TeamLeadMerchantAccrual).where(
            TeamLeadMerchantAccrual.teamlead_id == teamlead_id
        )
    )).scalars().all()
    all_accrual_rows = [*accrual_rows, *merchant_accrual_rows]
    settlements = (await db.execute(
        select(TeamLeadSettlement).where(
            TeamLeadSettlement.teamlead_id == teamlead_id
        )
    )).scalars().all()
    ledger_rows = (await db.execute(
        select(TeamLeadLedgerEntry)
        .where(TeamLeadLedgerEntry.teamlead_id == teamlead_id)
        .order_by(TeamLeadLedgerEntry.created_at, TeamLeadLedgerEntry.id)
    )).scalars().all()

    total_accrual = sum(
        (_money(row.accrual_rub) for row in all_accrual_rows),
        Decimal('0.00'),
    )
    trader_accrual = sum(
        (_money(row.accrual_rub) for row in accrual_rows),
        Decimal('0.00'),
    )
    merchant_accrual = sum(
        (_money(row.accrual_rub) for row in merchant_accrual_rows),
        Decimal('0.00'),
    )
    reversed_accrual = sum(
        (
            _money(row.accrual_rub)
            for row in all_accrual_rows
            if row.status == TeamLeadAccrualStatus.reversed.value
        ),
        Decimal('0.00'),
    )
    debt_offsets = sum(
        (_money(row.applied_to_debt_rub) for row in all_accrual_rows),
        Decimal('0.00'),
    )
    completed_total = sum(
        (
            _money(row.requested_rub)
            for row in settlements
            if row.status == TeamLeadSettlementStatus.completed.value
        ),
        Decimal('0.00'),
    )
    completed_fee_total = sum(
        (
            _money(row.fee_rub)
            for row in settlements
            if row.status == TeamLeadSettlementStatus.completed.value
        ),
        Decimal('0.00'),
    )
    pending_total = sum(
        (
            _money(row.total_debit_rub)
            for row in settlements
            if row.status == TeamLeadSettlementStatus.pending.value
        ),
        Decimal('0.00'),
    )
    released_total = sum(
        (
            _money(row.total_debit_rub)
            for row in settlements
            if row.status == TeamLeadSettlementStatus.rejected.value
        ),
        Decimal('0.00'),
    )
    cache = {
        'available_rub': _money(balance.available_rub),
        'frozen_rub': _money(balance.frozen_rub),
        'debt_rub': _money(balance.debt_rub),
        'total_earned_rub': _money(balance.total_earned_rub),
        'total_paid_rub': _money(balance.total_paid_rub),
    }
    expected_earned_from_accruals = _money(total_accrual - reversed_accrual)
    manual_net = Decimal('0.00')
    unrecognized_manual_entries: list[str] = []
    for row in ledger_rows:
        if row.entry_type == TeamLeadLedgerType.write_off.value:
            manual_net += _money(row.amount_rub)
        elif row.entry_type == TeamLeadLedgerType.manual_adjustment.value:
            direction = str(row.reason or '').split(':', 1)[0]
            if direction == 'available_credit':
                manual_net += _money(row.amount_rub)
            elif direction in {'available_debit', 'debt_increase'}:
                manual_net -= _money(row.amount_rub)
            else:
                unrecognized_manual_entries.append(str(row.id))
    cache_equation = _signed_money(
        cache['available_rub']
        + cache['frozen_rub']
        - cache['debt_rub']
        + cache['total_paid_rub']
        + completed_fee_total
    )
    ledger_matches_cache = (
        not ledger_rows
        and all(cache[key] == Decimal('0.00') for key in (
            'available_rub',
            'frozen_rub',
            'debt_rub',
        ))
    ) or any(
        _money(row.available_after) == cache['available_rub']
        and _money(row.frozen_after) == cache['frozen_rub']
        and _money(row.debt_after) == cache['debt_rub']
        for row in ledger_rows
    )
    checks = {
        'earned_matches_accruals': (
            cache['total_earned_rub'] == expected_earned_from_accruals
        ),
        'paid_matches_completed_settlements': cache['total_paid_rub'] == completed_total,
        'frozen_matches_pending_settlements': cache['frozen_rub'] == pending_total,
        'balance_equation': (
            cache_equation
            == _signed_money(cache['total_earned_rub'] + manual_net)
        ),
        'manual_adjustments_recognized': not unrecognized_manual_entries,
        'ledger_contains_current_cache_state': ledger_matches_cache,
        'ledger_idempotency_unique_in_report': len({
            row.idempotency_key for row in ledger_rows
        }) == len(ledger_rows),
    }
    return {
        'teamlead_id': str(teamlead_id),
        'totals': {
            'total_accrual_rub': _money(total_accrual),
            'trader_referral_accrual_rub': _money(trader_accrual),
            'merchant_referral_accrual_rub': _money(merchant_accrual),
            'reversed_accrual_rub': _money(reversed_accrual),
            'debt_offsets_rub': _money(debt_offsets),
            'completed_settlements_rub': _money(completed_total),
            'pending_settlements_rub': _money(pending_total),
            'rejected_releases_rub': _money(released_total),
            'manual_net_rub': _signed_money(manual_net),
        },
        'balance_cache': cache,
        'ledger_entries': len(ledger_rows),
        'checks': checks,
        'reconciled': all(checks.values()),
    }
