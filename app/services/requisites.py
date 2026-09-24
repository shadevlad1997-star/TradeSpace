from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import DepositStatus, PaymentMethod, Role
from app.core.payment_methods import normalize_payment_method, payment_method_label
from app.core.requisite_providers import requisite_provider_payload, resolve_requisite_provider
from app.core.security import reveal_text
from app.services.antiscam import can_route_payment_to_requisite, can_route_payment_to_trader
from app.services.ledger import LedgerError, trader_debit_frozen, trader_hold, trader_release_hold
from app.services.risk import destination_blacklist_hit
from app.models import Deposit, FeeRule, Requisite, User
from app.services.fee_tiers import FeeConfigurationError, resolve_fee_rule, validate_margin


ACTIVE_DEPOSIT_STATUSES = [
    DepositStatus.created.value,
    DepositStatus.pending.value,
    DepositStatus.appeal_opened.value,
]
COUNTED_DEPOSIT_STATUSES = ACTIVE_DEPOSIT_STATUSES + [DepositStatus.paid.value]


def deposit_method_candidates(method: str | None) -> list[str]:
    normalized = normalize_payment_method(method, canonical_mobile=True) or ''
    if normalized == PaymentMethod.sbp.value:
        return [PaymentMethod.sbp.value]
    if normalized == PaymentMethod.c2c.value:
        return [PaymentMethod.c2c.value]
    if normalized == PaymentMethod.mobile_commerce.value:
        return [PaymentMethod.mobile_commerce.value, PaymentMethod.mobile.value]
    return []


@dataclass
class SelectedPaymentRequisite:
    requisite: Requisite
    value: str
    exact_amount: Decimal
    trader_id: str | None = None
    trader_commission_percent: Decimal = Decimal('0.00')
    trader_hold_amount: Decimal = Decimal('0.00')
    trader_profit_amount: Decimal = Decimal('0.00')
    executor_rate_rule: FeeRule | None = None
    executor_type: str = 'trader'
    executor_id: str | None = None

    def payment_details(self) -> dict:
        method = normalize_payment_method(self.requisite.method, canonical_mobile=True) or self.requisite.method
        provider = requisite_provider_payload(
            method,
            self.requisite.bank_code,
            self.requisite.operator_code,
            self.requisite.bank_name,
        )
        return {
            'receiver_name': self.requisite.full_name or self.requisite.owner_name,
            'requisite': self.value,
            'bank': provider.get('bank', ''),
            'bank_name': (
                provider.get('bank', '')
                if provider.get('provider_type') == 'bank'
                else ''
            ),
            'exact_amount': str(self.exact_amount),
            'currency': 'RUB',
            'method': method,
            'payment_method': method,
            'method_label': payment_method_label(method),
            **provider,
        }

    def settlement_metadata(self) -> dict:
        return {
            'trader_id': self.trader_id,
            'trader_commission_percent': str(_percent(self.trader_commission_percent)),
            'trader_hold_amount': str(_money(self.trader_hold_amount)),
            'trader_profit_amount': str(_money(self.trader_profit_amount)),
            'trader_hold_status': 'active',
            'trader_settled': False,
            'trader_hold_released': False,
        }


def _day_start() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _window_start(timeframe: str) -> datetime:
    now = datetime.now(timezone.utc)
    if timeframe == 'день':
        return _day_start()
    return now - timedelta(hours=1)


def _assigned_to_merchant(trader: User | None, merchant_id: UUID) -> bool:
    if not trader:
        return True
    if trader.role not in [Role.operator.value, Role.trader.value]:
        return False
    assigned = trader.trader_assigned_merchants or []
    if not assigned:
        return True
    return str(merchant_id) in {str(item) for item in assigned}


def _money(value: Decimal | int | str | None) -> Decimal:
    return Decimal(value or '0.00').quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _percent(value: Decimal | int | str | None) -> Decimal:
    raw = Decimal(value or '0.00')
    if raw < Decimal('0'):
        raw = Decimal('0')
    if raw > Decimal('100'):
        raw = Decimal('100')
    return raw.quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)


def _commission_percent_for_trader(trader: User | None) -> Decimal:
    if not trader:
        return Decimal('0.00')
    return _percent(getattr(trader, 'trader_commission_percent', Decimal('0.00')))


def trader_profit_amount(amount: Decimal, commission_percent: Decimal) -> Decimal:
    return _money(_money(amount) * _percent(commission_percent) / Decimal('100'))


def trader_settlement_amount(amount: Decimal, commission_percent: Decimal) -> Decimal:
    return max(Decimal('0.00'), _money(_money(amount) - trader_profit_amount(amount, commission_percent)))


def deposit_trader_hold_amount(deposit: Deposit, trader: User | None = None) -> Decimal:
    meta = deposit.metadata_json or {}
    stored = meta.get('trader_hold_amount') or meta.get('trader_settlement_amount')
    if stored is not None:
        return _money(stored)
    # Legacy deposits were held by the full deposit amount before trader commission existed.
    return _money(deposit.amount)


def deposit_trader_profit_amount(deposit: Deposit, trader: User | None = None) -> Decimal:
    meta = deposit.metadata_json or {}
    stored = meta.get('trader_profit_amount')
    if stored is not None:
        return _money(stored)
    return trader_profit_amount(_money(deposit.amount), _commission_percent_for_trader(trader))


def deposit_trader_settlement_amount(deposit: Deposit, trader: User | None = None) -> Decimal:
    meta = deposit.metadata_json or {}
    stored = meta.get('trader_hold_amount') or meta.get('trader_settlement_amount')
    if stored is not None:
        return _money(stored)
    return trader_settlement_amount(_money(deposit.amount), _commission_percent_for_trader(trader))


def _set_deposit_metadata(deposit: Deposit, **values) -> None:
    metadata = dict(deposit.metadata_json or {})
    metadata.update(values)
    deposit.metadata_json = metadata


def _trader_available(trader: User) -> Decimal:
    return _money(trader.trader_balance) - _money(trader.trader_hold)


def _can_use_trader(
    trader: User | None,
    merchant_id: UUID,
    amount: Decimal,
    commission_percent: Decimal | None = None,
) -> bool:
    if trader is None:
        return False
    if not _assigned_to_merchant(trader, merchant_id):
        return False
    effective_percent = (
        _commission_percent_for_trader(trader)
        if commission_percent is None
        else _percent(commission_percent)
    )
    settlement_amount = trader_settlement_amount(amount, effective_percent)
    return _trader_available(trader) >= settlement_amount


def success_delay_elapsed(req: Requisite, now: datetime | None = None) -> bool:
    delay_minutes = int(req.success_delay_minutes or 0)
    if delay_minutes <= 0 or not req.last_success_at:
        return True
    current = now or datetime.now(timezone.utc)
    last_success = req.last_success_at if req.last_success_at.tzinfo else req.last_success_at.replace(tzinfo=timezone.utc)
    return current - last_success >= timedelta(minutes=delay_minutes)


async def settle_trader_deposit_balance(db: AsyncSession, deposit: Deposit) -> User | None:
    if deposit.status not in ACTIVE_DEPOSIT_STATUSES:
        raise ValueError(f'deposit status {deposit.status} cannot be settled')
    if not deposit.requisites_id:
        return None
    req = (await db.execute(
        select(Requisite).where(Requisite.id == deposit.requisites_id).with_for_update()
    )).scalar_one_or_none()
    if not req or not req.trader_id:
        return None
    trader = (await db.execute(
        select(User)
        .where(User.id == req.trader_id, User.role.in_([Role.operator.value, Role.trader.value]))
        .with_for_update()
    )).scalar_one_or_none()
    if not trader:
        return None

    metadata = dict(deposit.metadata_json or {})
    hold_status = metadata.get('trader_hold_status')
    if hold_status == 'settled' or metadata.get('trader_settled') is True:
        raise ValueError('trader deposit settlement has already been applied')
    if hold_status == 'released' or metadata.get('trader_hold_released') is True:
        raise ValueError('trader deposit hold has already been released')

    hold_amount = deposit_trader_hold_amount(deposit, trader)
    settlement_amount = deposit_trader_settlement_amount(deposit, trader)
    if _money(trader.trader_hold) < hold_amount:
        raise ValueError('trader hold is lower than confirmed deposit hold amount')
    if _money(trader.trader_balance) < settlement_amount:
        raise ValueError('trader balance is lower than confirmed deposit settlement amount')
    try:
        await trader_debit_frozen(
            db,
            trader,
            settlement_amount,
            deposit.id,
            f'trader-deposit-settle:{deposit.id}',
            'successful deposit settlement debit',
        )
    except LedgerError as exc:
        raise ValueError(str(exc)) from exc
    extra_hold = _money(hold_amount - settlement_amount)
    if extra_hold > 0:
        try:
            await trader_release_hold(
                db,
                trader,
                extra_hold,
                deposit.id,
                f'trader-deposit-profit-release:{deposit.id}',
                'successful deposit trader commission release',
            )
        except LedgerError as exc:
            raise ValueError(str(exc)) from exc
    req.last_success_at = datetime.now(timezone.utc)
    _set_deposit_metadata(
        deposit,
        trader_hold_status='settled',
        trader_settled=True,
        trader_settled_at=datetime.now(timezone.utc).isoformat(),
    )
    return trader


class DepositReserveInconsistency(ValueError):
    """The persisted reserve cannot fund this operation; never repair implicitly."""
    code = 'deposit_reserve_inconsistent'


async def release_trader_deposit_hold(db: AsyncSession, deposit: Deposit) -> User | None:
    metadata = dict(deposit.metadata_json or {})
    if metadata.get('trader_hold_status') in {'released', 'settled'} or metadata.get('trader_hold_released') is True:
        return None
    if not deposit.requisites_id:
        return None
    req = (await db.execute(
        select(Requisite).where(Requisite.id == deposit.requisites_id).with_for_update()
    )).scalar_one_or_none()
    if not req or not req.trader_id:
        return None
    trader = (await db.execute(
        select(User)
        .where(User.id == req.trader_id, User.role.in_([Role.operator.value, Role.trader.value]))
        .with_for_update()
    )).scalar_one_or_none()
    if not trader:
        return None
    hold_amount = deposit_trader_hold_amount(deposit, trader)
    try:
        await trader_release_hold(
            db,
            trader,
            hold_amount,
            deposit.id,
            f'trader-deposit-release:{deposit.id}',
            'deposit hold released',
        )
    except LedgerError as exc:
        raise DepositReserveInconsistency(str(exc)) from exc
    _set_deposit_metadata(
        deposit,
        trader_hold_status='released',
        trader_hold_released=True,
        trader_hold_released_at=datetime.now(timezone.utc).isoformat(),
    )
    return trader


async def select_deposit_requisite(
    db: AsyncSession,
    *,
    merchant_id: UUID,
    method: str,
    amount: Decimal,
    merchant_rate_rule: FeeRule,
    executor_type: str = 'trader',
    executor_rule: FeeRule | None = None,
    executor_id: UUID | None = None,
) -> SelectedPaymentRequisite:
    method_candidates = deposit_method_candidates(method)
    if not method_candidates:
        raise ValueError('payment method is not supported for player deposits')
    method = method_candidates[0]

    candidates = (await db.execute(
        select(Requisite)
        .where(
            Requisite.method.in_(method_candidates),
            Requisite.enabled == True,
            Requisite.status == 'active',
            Requisite.is_archived == False,
            Requisite.min_check <= amount,
            Requisite.max_check >= amount,
        )
        .order_by(Requisite.usage_count.asc(), Requisite.created_at.asc())
        .with_for_update()
    )).scalars().all()
    if not candidates:
        raise ValueError(f'Нет активных реквизитов для requested payment_method: {method}')

    trader_ids = [item.trader_id for item in candidates if item.trader_id]
    traders: dict[str, User] = {}
    if trader_ids:
        rows = (await db.execute(
            select(User)
            .where(
                User.id.in_(trader_ids),
                User.is_active == True,
                User.is_locked == False,
                User.is_archived == False,
                User.role.in_([Role.operator.value, Role.trader.value]),
            )
            .with_for_update()
        )).scalars().all()
        traders = {str(row.id): row for row in rows}

    candidates = sorted(
        candidates,
        key=lambda req: (
            -int(getattr(traders.get(str(req.trader_id)), 'trader_traffic_priority', 0) or 0),
            int(req.usage_count or 0),
            req.created_at,
        ),
    )

    now = datetime.now(timezone.utc)
    day_start = _day_start()
    amount = _money(amount)
    last_fee_error: FeeConfigurationError | None = None
    for req in candidates:
        if not resolve_requisite_provider(req.method, bank_code=req.bank_code, operator_code=req.operator_code, bank_name=req.bank_name, allow_legacy=True):
            continue

        trader = traders.get(str(req.trader_id)) if req.trader_id else None
        selected_executor_rule = executor_rule
        selected_executor_id = executor_id
        if executor_type == 'trader':
            try:
                selected_executor_rule = await resolve_fee_rule(
                    db,
                    entity_type='trader',
                    entity_id=trader.id if trader else '',
                    fee_side='executor_fee',
                    payment_method=method,
                    currency='RUB',
                    amount=amount,
                    at=now,
                )
                validate_margin(merchant_rate_rule, selected_executor_rule)
            except FeeConfigurationError as exc:
                last_fee_error = exc
                continue
            selected_executor_id = trader.id if trader else None
            commission_percent = Decimal(selected_executor_rule.rate_percent)
        elif executor_type == 'aggregator' and selected_executor_rule and selected_executor_id:
            validate_margin(merchant_rate_rule, selected_executor_rule)
            commission_percent = Decimal('0.00')
        else:
            raise FeeConfigurationError('invalid or missing executor fee rule')

        if not _can_use_trader(trader, merchant_id, amount, commission_percent):
            continue

        if not await can_route_payment_to_trader(db, trader, amount):
            continue

        if not await can_route_payment_to_requisite(db, req, trader, amount):
            continue

        if not success_delay_elapsed(req, now):
            continue

        active_count = (await db.execute(
            select(func.count()).select_from(Deposit).where(
                Deposit.requisites_id == req.id,
                Deposit.status.in_(ACTIVE_DEPOSIT_STATUSES),
            )
        )).scalar_one()
        if req.simultaneous_limit and active_count >= req.simultaneous_limit:
            continue

        today_count = (await db.execute(
            select(func.count()).select_from(Deposit).where(
                Deposit.requisites_id == req.id,
                Deposit.status.in_(COUNTED_DEPOSIT_STATUSES),
                Deposit.created_at >= day_start,
            )
        )).scalar_one()
        if req.operation_limit and today_count >= req.operation_limit:
            continue

        today_sum = (await db.execute(
            select(func.coalesce(func.sum(Deposit.amount), 0)).where(
                Deposit.requisites_id == req.id,
                Deposit.status.in_(COUNTED_DEPOSIT_STATUSES),
                Deposit.created_at >= day_start,
            )
        )).scalar_one()
        if req.daily_limit and req.daily_limit > 0 and Decimal(today_sum) + amount > req.daily_limit:
            continue

        window_start = _window_start(req.timeframe)
        window_count = (await db.execute(
            select(func.count()).select_from(Deposit).where(
                Deposit.requisites_id == req.id,
                Deposit.status.in_(COUNTED_DEPOSIT_STATUSES),
                Deposit.created_at >= window_start,
                Deposit.created_at <= now,
            )
        )).scalar_one()
        if req.request_count and window_count >= req.request_count:
            continue

        requisite_value = reveal_text(req.value_encrypted).strip()
        if not requisite_value:
            continue
        if await destination_blacklist_hit(db, requisite_value):
            continue

        profit_amount = trader_profit_amount(amount, commission_percent)
        hold_amount = trader_settlement_amount(amount, commission_percent)
        try:
            await trader_hold(db, trader, hold_amount, req.id, f'trader-deposit-hold:{trader.id}:{req.id}:{(req.usage_count or 0) + 1}', 'deposit requisite reserved')
        except LedgerError:
            continue
        req.usage_count += 1
        return SelectedPaymentRequisite(
            req,
            requisite_value,
            amount,
            trader_id=str(trader.id),
            trader_commission_percent=commission_percent,
            trader_hold_amount=hold_amount,
            trader_profit_amount=profit_amount,
            executor_rate_rule=selected_executor_rule,
            executor_type=executor_type,
            executor_id=str(selected_executor_id),
        )

    if last_fee_error is not None:
        raise last_fee_error
    raise ValueError('all matching requisites are busy, over limits, or assigned traders have insufficient available balance')
