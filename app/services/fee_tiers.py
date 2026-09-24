from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import UUID

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.payment_methods import normalize_payment_method
from app.models import AggregatorAccount, Deposit, FeeRule, Merchant, OperationFeeSnapshot, User


MONEY_QUANT = Decimal('0.01')
PERCENT_QUANT = Decimal('0.0001')
SUPPORTED_ENTITY_TYPES = {'merchant', 'trader', 'aggregator'}
SUPPORTED_FEE_SIDES = {'merchant_fee', 'executor_fee'}
SUPPORTED_CURRENCIES = {'RUB'}
MAX_RATE_PERCENT = Decimal('100.0000')
COMMISSION_TIER_METHODS = ('sbp', 'c2c', 'mobile_commerce')
COMMISSION_TIER_RANGES = (
    {
        'key': '100_999',
        'field': 'rate_100_999',
        'label': '100–999 ₽',
        'min_amount': Decimal('100.00'),
        'max_amount': Decimal('1000.00'),
    },
    {
        'key': '1000_4999',
        'field': 'rate_1000_4999',
        'label': '1 000–4 999 ₽',
        'min_amount': Decimal('1000.00'),
        'max_amount': Decimal('5000.00'),
    },
    {
        'key': '5000_9999',
        'field': 'rate_5000_9999',
        'label': '5 000–9 999 ₽',
        'min_amount': Decimal('5000.00'),
        'max_amount': Decimal('10000.00'),
    },
    {
        'key': '10000_150000',
        'field': 'rate_10000_150000',
        'label': '10 000–150 000 ₽',
        'min_amount': Decimal('10000.00'),
        'max_amount': Decimal('150000.01'),
    },
)


class FeeConfigurationError(ValueError):
    code = 'fee_configuration_error'


class NegativePlatformMarginError(FeeConfigurationError):
    code = 'negative_platform_margin'


class FeeRuleOverlapError(FeeConfigurationError):
    code = 'fee_rule_overlap'


class FeeRuleNotFoundError(FeeConfigurationError):
    code = 'fee_rule_missing'


class FeeRuleAmbiguousError(FeeConfigurationError):
    code = 'fee_rule_ambiguous'


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def money(value: Decimal | int | str) -> Decimal:
    try:
        result = Decimal(str(value)).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise FeeConfigurationError('invalid money value') from exc
    if not result.is_finite():
        raise FeeConfigurationError('invalid money value')
    return result


def rate(value: Decimal | int | str) -> Decimal:
    try:
        result = Decimal(str(value)).quantize(PERCENT_QUANT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise FeeConfigurationError('invalid rate percent') from exc
    if not result.is_finite() or result < 0 or result > MAX_RATE_PERCENT:
        raise FeeConfigurationError('rate_percent must be between 0 and 100')
    return result


def commission_rate(value: Decimal | int | str) -> Decimal:
    raw = str(value if value is not None else '').strip().replace(',', '.')
    if not raw or not re.fullmatch(r'\d+(?:\.\d{1,2})?', raw):
        raise FeeConfigurationError('commission percent must be a decimal with at most 2 fractional digits')
    try:
        parsed = Decimal(raw)
    except InvalidOperation as exc:
        raise FeeConfigurationError('invalid commission percent') from exc
    if not parsed.is_finite() or parsed < 0 or parsed > Decimal('100'):
        raise FeeConfigurationError('commission percent must be between 0 and 100')
    return parsed.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def fee_amount(amount: Decimal, rate_percent: Decimal) -> Decimal:
    return (money(amount) * rate(rate_percent) / Decimal('100')).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def normalize_method(value: str) -> str:
    normalized = normalize_payment_method(value, canonical_mobile=True)
    if not normalized:
        raise FeeConfigurationError('unknown payment method')
    return normalized


def normalize_currency(value: str) -> str:
    normalized = (value or '').strip().upper()
    if normalized not in SUPPORTED_CURRENCIES:
        raise FeeConfigurationError('unknown currency')
    return normalized


def normalize_scope(entity_type: str, entity_id: UUID | str, fee_side: str) -> tuple[str, UUID, str]:
    entity_type = (entity_type or '').strip().lower()
    fee_side = (fee_side or '').strip().lower()
    if entity_type not in SUPPORTED_ENTITY_TYPES:
        raise FeeConfigurationError('unknown fee entity type')
    if fee_side not in SUPPORTED_FEE_SIDES:
        raise FeeConfigurationError('unknown fee side')
    if fee_side == 'merchant_fee' and entity_type != 'merchant':
        raise FeeConfigurationError('merchant_fee requires merchant entity')
    if fee_side == 'executor_fee' and entity_type not in {'trader', 'aggregator'}:
        raise FeeConfigurationError('executor_fee requires trader or aggregator entity')
    try:
        parsed_id = UUID(str(entity_id))
    except (TypeError, ValueError) as exc:
        raise FeeConfigurationError('invalid fee entity id') from exc
    if parsed_id.int == 0:
        raise FeeConfigurationError('fee entity id must not be the zero UUID')
    return entity_type, parsed_id, fee_side


def validate_rule_values(
    *,
    entity_type: str,
    entity_id: UUID | str,
    fee_side: str,
    payment_method: str,
    currency: str,
    min_amount: Decimal | str,
    max_amount: Decimal | str | None,
    rate_percent: Decimal | str,
    effective_from: datetime,
    effective_to: datetime | None,
) -> dict:
    normalized_entity_type, normalized_entity_id, normalized_fee_side = normalize_scope(entity_type, entity_id, fee_side)
    minimum = money(min_amount)
    maximum = money(max_amount) if max_amount not in (None, '') else None
    if minimum < 0:
        raise FeeConfigurationError('min_amount must be nonnegative')
    if maximum is not None and maximum <= minimum:
        raise FeeConfigurationError('max_amount must be greater than min_amount')
    if effective_from is None:
        raise FeeConfigurationError('effective_from is required')
    if effective_from.tzinfo is None:
        effective_from = effective_from.replace(tzinfo=timezone.utc)
    if effective_to is not None:
        if effective_to.tzinfo is None:
            effective_to = effective_to.replace(tzinfo=timezone.utc)
        if effective_to <= effective_from:
            raise FeeConfigurationError('effective_to must be greater than effective_from')
    return {
        'entity_type': normalized_entity_type,
        'entity_id': normalized_entity_id,
        'fee_side': normalized_fee_side,
        'payment_method': normalize_method(payment_method),
        'currency': normalize_currency(currency),
        'min_amount': minimum,
        'max_amount': maximum,
        'rate_percent': rate(rate_percent),
        'effective_from': effective_from,
        'effective_to': effective_to,
    }


def amount_ranges_overlap(a_min: Decimal, a_max: Decimal | None, b_min: Decimal, b_max: Decimal | None) -> bool:
    return (a_max is None or b_min < a_max) and (b_max is None or a_min < b_max)


def effective_periods_overlap(a_from: datetime, a_to: datetime | None, b_from: datetime, b_to: datetime | None) -> bool:
    return (a_to is None or b_from < a_to) and (b_to is None or a_from < b_to)


async def _lock_rule_scope(db: AsyncSession, values: dict) -> None:
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == 'postgresql':
        scope = '|'.join(
            str(values[key])
            for key in ('entity_type', 'entity_id', 'fee_side', 'payment_method', 'currency')
        )
        await db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))'), {'scope': scope})


async def ensure_no_overlap(db: AsyncSession, values: dict, *, exclude_id: UUID | None = None) -> None:
    await _lock_rule_scope(db, values)
    query = select(FeeRule).where(
        FeeRule.entity_type == values['entity_type'],
        FeeRule.entity_id == values['entity_id'],
        FeeRule.fee_side == values['fee_side'],
        FeeRule.payment_method == values['payment_method'],
        FeeRule.currency == values['currency'],
        FeeRule.is_active == True,
    ).with_for_update()
    if exclude_id is not None:
        query = query.where(FeeRule.id != exclude_id)
    existing = (await db.execute(query)).scalars().all()
    for rule in existing:
        if amount_ranges_overlap(
            values['min_amount'], values['max_amount'], Decimal(rule.min_amount), Decimal(rule.max_amount) if rule.max_amount is not None else None,
        ) and effective_periods_overlap(
            values['effective_from'], values['effective_to'], rule.effective_from, rule.effective_to,
        ):
            raise FeeRuleOverlapError('fee rule amount/effective period overlaps an active rule')


async def ensure_entity_exists(db: AsyncSession, values: dict) -> None:
    if values['entity_type'] == 'merchant':
        query = select(Merchant.id).join(User, User.id == Merchant.owner_id).where(
            Merchant.id == values['entity_id'],
            Merchant.is_archived.is_(False),
            User.role == 'merchant',
            User.is_active.is_(True),
            User.is_locked.is_(False),
            User.is_archived.is_(False),
        )
    elif values['entity_type'] == 'trader':
        query = select(User.id).where(
            User.id == values['entity_id'],
            User.role.in_(['trader', 'operator']),
            User.is_active.is_(True),
            User.is_locked.is_(False),
            User.is_archived.is_(False),
        )
    else:
        query = select(AggregatorAccount.id).where(
            AggregatorAccount.id == values['entity_id'],
            AggregatorAccount.status == 'active',
            AggregatorAccount.is_archived.is_(False),
        )
    if (await db.execute(query)).scalar_one_or_none() is None:
        raise FeeConfigurationError('fee entity does not exist or is inactive')


async def create_fee_rule(
    db: AsyncSession,
    *,
    actor_id: UUID,
    version: int = 1,
    is_active: bool = True,
    **raw_values,
) -> FeeRule:
    values = validate_rule_values(**raw_values)
    await ensure_entity_exists(db, values)
    if is_active:
        await ensure_no_overlap(db, values)
    rule = FeeRule(
        merchant_id=values['entity_id'] if values['entity_type'] == 'merchant' else None,
        method=values['payment_method'],
        percent=values['rate_percent'],
        fixed=Decimal('0.00'),
        **values,
        is_active=bool(is_active),
        version=max(1, int(version)),
        created_by=actor_id,
        updated_by=actor_id,
    )
    db.add(rule)
    await db.flush()
    return rule


async def replace_fee_rule(db: AsyncSession, current: FeeRule, *, actor_id: UUID, **raw_values) -> FeeRule:
    values = validate_rule_values(**raw_values)
    if (
        values['entity_type'] != current.entity_type
        or values['entity_id'] != current.entity_id
        or values['fee_side'] != current.fee_side
    ):
        raise FeeConfigurationError('fee rule scope cannot be changed between versions')
    await ensure_entity_exists(db, values)
    await ensure_no_overlap(db, values, exclude_id=current.id)
    current.is_active = False
    current.effective_to = min(current.effective_to, utcnow()) if current.effective_to else utcnow()
    current.updated_by = actor_id
    return await create_fee_rule(db, actor_id=actor_id, version=int(current.version or 1) + 1, **values)


def _matches_commission_tier(rule: FeeRule, tier: dict) -> bool:
    return (
        money(rule.min_amount) == tier['min_amount']
        and rule.max_amount is not None
        and money(rule.max_amount) == tier['max_amount']
    )


def build_commission_tier_context(rules: list[FeeRule]) -> dict[str, dict[str, dict[str, dict]]]:
    context: dict[str, dict[str, dict[str, dict]]] = {}
    for rule in rules:
        if not rule.is_active or rule.currency != 'RUB' or rule.payment_method not in COMMISSION_TIER_METHODS:
            continue
        tier = next((candidate for candidate in COMMISSION_TIER_RANGES if _matches_commission_tier(rule, candidate)), None)
        if tier is None:
            continue
        entity_key = f'{rule.entity_type}:{rule.entity_id}'
        method_state = context.setdefault(entity_key, {}).setdefault(rule.payment_method, {})
        existing = method_state.get(tier['key'])
        if existing and int(existing['version']) >= int(rule.version or 1):
            continue
        method_state[tier['key']] = {
            'configured': True,
            'rate_percent': f'{Decimal(rule.rate_percent):.2f}',
            'version': int(rule.version or 1),
            'updated_at': (rule.updated_at or rule.created_at).isoformat() if (rule.updated_at or rule.created_at) else '',
        }
    return context


async def replace_commission_tiers(
    db: AsyncSession,
    *,
    actor_id: UUID,
    entity_type: str,
    entity_id: UUID | str,
    payment_method: str,
    raw_rates: dict[str, Decimal | int | str],
    effective_at: datetime | None = None,
) -> tuple[list[dict], list[FeeRule]]:
    fee_side = 'merchant_fee' if entity_type == 'merchant' else 'executor_fee'
    entity_type, entity_id, fee_side = normalize_scope(entity_type, entity_id, fee_side)
    payment_method = (payment_method or 'sbp').strip().lower()
    if payment_method not in COMMISSION_TIER_METHODS:
        raise FeeConfigurationError('unsupported commission tier payment method')
    effective_at = effective_at or utcnow()
    if effective_at.tzinfo is None:
        effective_at = effective_at.replace(tzinfo=timezone.utc)

    parsed_rates = {
        tier['field']: commission_rate(raw_rates.get(tier['field'], ''))
        for tier in COMMISSION_TIER_RANGES
    }
    scope_values = validate_rule_values(
        entity_type=entity_type,
        entity_id=entity_id,
        fee_side=fee_side,
        payment_method=payment_method,
        currency='RUB',
        min_amount=COMMISSION_TIER_RANGES[0]['min_amount'],
        max_amount=COMMISSION_TIER_RANGES[0]['max_amount'],
        rate_percent=parsed_rates[COMMISSION_TIER_RANGES[0]['field']],
        effective_from=effective_at,
        effective_to=None,
    )
    await ensure_entity_exists(db, scope_values)
    await _lock_rule_scope(db, scope_values)
    existing_rules = (await db.execute(
        select(FeeRule).where(
            FeeRule.entity_type == entity_type,
            FeeRule.entity_id == entity_id,
            FeeRule.fee_side == fee_side,
            FeeRule.payment_method == payment_method,
            FeeRule.currency == 'RUB',
        ).with_for_update()
    )).scalars().all()
    old_values = [
        {
            'rule_id': str(rule.id),
            'min_amount': str(rule.min_amount),
            'max_amount': str(rule.max_amount) if rule.max_amount is not None else None,
            'rate_percent': str(rule.rate_percent),
            'version': int(rule.version or 1),
        }
        for rule in existing_rules
        if rule.is_active
    ]
    for rule in existing_rules:
        if not rule.is_active:
            continue
        rule.is_active = False
        if rule.effective_from < effective_at:
            rule.effective_to = min(rule.effective_to, effective_at) if rule.effective_to else effective_at
        rule.updated_by = actor_id
    await db.flush()

    new_rules: list[FeeRule] = []
    for tier in COMMISSION_TIER_RANGES:
        previous_versions = [
            int(rule.version or 1)
            for rule in existing_rules
            if _matches_commission_tier(rule, tier)
        ]
        new_rules.append(await create_fee_rule(
            db,
            actor_id=actor_id,
            version=max(previous_versions, default=0) + 1,
            entity_type=entity_type,
            entity_id=entity_id,
            fee_side=fee_side,
            payment_method=payment_method,
            currency='RUB',
            min_amount=tier['min_amount'],
            max_amount=tier['max_amount'],
            rate_percent=parsed_rates[tier['field']],
            effective_from=effective_at,
            effective_to=None,
        ))
    return old_values, new_rules


async def resolve_fee_rule(
    db: AsyncSession,
    *,
    entity_type: str,
    entity_id: UUID | str,
    fee_side: str,
    payment_method: str,
    currency: str,
    amount: Decimal | str,
    at: datetime | None = None,
) -> FeeRule:
    entity_type, entity_id, fee_side = normalize_scope(entity_type, entity_id, fee_side)
    payment_method = normalize_method(payment_method)
    currency = normalize_currency(currency)
    amount = money(amount)
    at = at or utcnow()
    rows = (await db.execute(
        select(FeeRule).where(
            FeeRule.entity_type == entity_type,
            FeeRule.entity_id == entity_id,
            FeeRule.fee_side == fee_side,
            FeeRule.payment_method == payment_method,
            FeeRule.currency == currency,
            FeeRule.is_active == True,
            FeeRule.min_amount <= amount,
            or_(FeeRule.max_amount.is_(None), FeeRule.max_amount > amount),
            FeeRule.effective_from <= at,
            or_(FeeRule.effective_to.is_(None), FeeRule.effective_to > at),
        ).order_by(FeeRule.version.desc(), FeeRule.created_at.desc())
    )).scalars().all()
    if not rows:
        raise FeeRuleNotFoundError(
            f'no active fee rule for {entity_type}:{entity_id} {fee_side} {payment_method} {currency} amount={amount}'
        )
    if len(rows) != 1:
        raise FeeRuleAmbiguousError('multiple active fee rules match the operation')
    return rows[0]


def validate_margin(merchant_rule: FeeRule, executor_rule: FeeRule) -> None:
    if rate(merchant_rule.rate_percent) < rate(executor_rule.rate_percent):
        raise NegativePlatformMarginError('merchant rate is lower than executor rate')


async def create_operation_fee_snapshot(
    db: AsyncSession,
    *,
    deposit: Deposit,
    merchant_rule: FeeRule,
    executor_rule: FeeRule,
    executor_type: str,
    executor_id: UUID | str,
) -> OperationFeeSnapshot:
    existing = (await db.execute(
        select(OperationFeeSnapshot).where(OperationFeeSnapshot.deposit_id == deposit.id)
    )).scalar_one_or_none()
    if existing:
        return existing
    if executor_type not in {'trader', 'aggregator'}:
        raise FeeConfigurationError('invalid settlement owner')
    executor_id = UUID(str(executor_id))
    if merchant_rule.entity_type != 'merchant' or merchant_rule.entity_id != deposit.merchant_id:
        raise FeeConfigurationError('merchant fee rule does not belong to the operation merchant')
    if merchant_rule.fee_side != 'merchant_fee':
        raise FeeConfigurationError('invalid merchant fee rule side')
    if executor_rule.entity_type != executor_type or executor_rule.entity_id != executor_id:
        raise FeeConfigurationError('executor fee rule does not belong to the operation executor')
    if executor_rule.fee_side != 'executor_fee':
        raise FeeConfigurationError('invalid executor fee rule side')
    validate_margin(merchant_rule, executor_rule)
    amount = money(deposit.amount)
    merchant_rate = rate(merchant_rule.rate_percent)
    executor_rate = rate(executor_rule.rate_percent)
    merchant_fee = fee_amount(amount, merchant_rate)
    executor_fee = fee_amount(amount, executor_rate)
    platform_income = money(merchant_fee - executor_fee)
    if platform_income < 0:
        raise NegativePlatformMarginError('negative platform margin')
    snapshot = OperationFeeSnapshot(
        deposit_id=deposit.id,
        merchant_id=deposit.merchant_id,
        merchant_rate_rule_id=merchant_rule.id,
        merchant_rate_version=merchant_rule.version,
        merchant_rate_percent=merchant_rate,
        merchant_fee_amount=merchant_fee,
        executor_type=executor_type,
        executor_id=executor_id,
        executor_rate_rule_id=executor_rule.id,
        executor_rate_version=executor_rule.version,
        executor_rate_percent=executor_rate,
        executor_fee_amount=executor_fee,
        platform_margin_percent=rate(merchant_rate - executor_rate),
        platform_income_amount=platform_income,
        calculation_base_amount=amount,
        currency=normalize_currency(deposit.currency),
        payment_method=normalize_method(deposit.method),
        rate_snapshot_at=utcnow(),
    )
    db.add(snapshot)
    await db.flush()
    return snapshot


async def get_operation_fee_snapshot(db: AsyncSession, deposit_id: UUID, *, lock: bool = False) -> OperationFeeSnapshot:
    query = select(OperationFeeSnapshot).where(OperationFeeSnapshot.deposit_id == deposit_id)
    if lock:
        query = query.with_for_update()
    snapshot = (await db.execute(query)).scalar_one_or_none()
    if not snapshot:
        raise FeeConfigurationError('operation fee snapshot is missing')
    return snapshot
