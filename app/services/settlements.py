from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.tron import TronAddressError, validate_trc20_address
from app.models import Balance, MerchantSettlement
from app.services.audit import audit
from app.services.ledger import (
    LedgerError,
    debit_frozen,
    get_or_create_balance,
    hold,
    release_hold,
)
from app.services.rapira import (
    RollingRapiraQuote,
    RollingRateUnavailable,
    get_strict_rolling_ask_quote,
)


SETTLEMENT_STATUS_PENDING = 'pending'
SETTLEMENT_STATUS_COMPLETED = 'completed'
SETTLEMENT_STATUS_REJECTED = 'rejected'
SETTLEMENT_NETWORK = 'TRC20'
TWOPLACES = Decimal('0.01')
FOURPLACES = Decimal('0.0001')
MAX_IDEMPOTENCY_KEY_LENGTH = 180
MAX_TX_HASH_LENGTH = 160
FUTURE_SKEW = timedelta(seconds=5)


class MerchantSettlementError(ValueError):
    code = 'merchant_settlement_error'

    def __init__(self, code: str | None = None):
        self.code = code or self.code
        super().__init__(self.code)


class MerchantSettlementRateUnavailable(MerchantSettlementError):
    code = 'merchant_settlement_rate_unavailable'


class MerchantSettlementConflict(MerchantSettlementError):
    code = 'merchant_settlement_conflict'


def money(value: Decimal | int | str | None) -> Decimal:
    try:
        parsed = Decimal(value or '0.00')
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MerchantSettlementError('invalid_money') from exc
    if not parsed.is_finite():
        raise MerchantSettlementError('invalid_money')
    return parsed.quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def usdt(value: Decimal | int | str | None) -> Decimal:
    try:
        parsed = Decimal(value or '0.00')
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MerchantSettlementError('invalid_usdt_amount') from exc
    if not parsed.is_finite():
        raise MerchantSettlementError('invalid_usdt_amount')
    return parsed.quantize(TWOPLACES, rounding=ROUND_DOWN)


def rate(value: Decimal | int | str | None) -> Decimal:
    try:
        parsed = Decimal(value or '0.0000')
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MerchantSettlementRateUnavailable() from exc
    if not parsed.is_finite() or parsed <= 0:
        raise MerchantSettlementRateUnavailable()
    return parsed.quantize(FOURPLACES, rounding=ROUND_HALF_UP)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise MerchantSettlementRateUnavailable()
    return value.astimezone(timezone.utc)


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
        raise MerchantSettlementRateUnavailable()
    quote_rate = rate(quote.rate)
    fetched_at = _utc(quote.fetched_at)
    age = now - fetched_at
    max_age = timedelta(seconds=settings.ROLLING_RAPIRA_MAX_AGE_SECONDS)
    if age < -FUTURE_SKEW or age > max_age:
        raise MerchantSettlementRateUnavailable()
    if quote.freshness_basis == 'provider_timestamp':
        if quote.provider_timestamp is None:
            raise MerchantSettlementRateUnavailable()
        provider_age = fetched_at - _utc(quote.provider_timestamp)
        if provider_age < -FUTURE_SKEW or provider_age > max_age:
            raise MerchantSettlementRateUnavailable()
    elif quote.provider_timestamp is not None:
        raise MerchantSettlementRateUnavailable()
    return quote_rate


async def _live_quote() -> RollingRapiraQuote:
    try:
        return await get_strict_rolling_ask_quote()
    except RollingRateUnavailable as exc:
        raise MerchantSettlementRateUnavailable() from exc


async def settlement_rate_rub() -> tuple[Decimal, str]:
    quote = await _live_quote()
    return (
        _validate_live_quote(quote, now=datetime.now(timezone.utc)),
        quote.source,
    )


def settlement_transfer_fee_usdt() -> Decimal:
    configured = usdt(settings.SETTLEMENT_TRANSFER_FEE_USDT)
    return configured if configured >= 0 else Decimal('5.00')


def _validated_address(address: str) -> str:
    try:
        return validate_trc20_address(address)
    except TronAddressError as exc:
        raise MerchantSettlementError(exc.code) from exc


def _idempotency_key(value: str) -> str:
    normalized = (value or '').strip()
    if not normalized:
        raise MerchantSettlementError('idempotency_key_required')
    if len(normalized) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise MerchantSettlementError('idempotency_key_too_long')
    return normalized


def _tx_hash(value: str) -> str:
    normalized = (value or '').strip()
    if not normalized:
        raise MerchantSettlementError('tx_hash_required')
    if len(normalized) > MAX_TX_HASH_LENGTH:
        raise MerchantSettlementError('tx_hash_too_long')
    return normalized


async def pending_settlement_total(
    db: AsyncSession,
    merchant_id: UUID,
) -> Decimal:
    total = (
        await db.execute(
            select(
                func.coalesce(
                    func.sum(MerchantSettlement.total_debit_rub),
                    0,
                )
            ).where(
                MerchantSettlement.merchant_id == merchant_id,
                MerchantSettlement.status == SETTLEMENT_STATUS_PENDING,
            )
        )
    ).scalar_one()
    return money(total)


async def merchant_settlement_quote(
    db: AsyncSession,
    merchant_id: UUID,
    *,
    allow_unavailable: bool = False,
) -> dict[str, Any]:
    balance = (
        await db.execute(
            select(Balance).where(
                Balance.merchant_id == merchant_id,
                Balance.currency == 'RUB',
            )
        )
    ).scalar_one_or_none()
    available = money(balance.available if balance else Decimal('0.00'))
    pending = await pending_settlement_total(db, merchant_id)
    fee_usdt = settlement_transfer_fee_usdt()
    try:
        live_quote = await _live_quote()
        current_rate = _validate_live_quote(
            live_quote,
            now=datetime.now(timezone.utc),
        )
    except MerchantSettlementRateUnavailable:
        if not allow_unavailable:
            raise
        return {
            'rate_available': False,
            'rate_rub': Decimal('0.0000'),
            'fee_usdt': fee_usdt,
            'fee_rub': Decimal('0.00'),
            'available_rub': max(Decimal('0.00'), available),
            'pending_rub': pending,
            'max_request_usdt': Decimal('0.00'),
            'rate_source': 'unavailable',
            'rate_updated_at': None,
            'rate_stale': True,
            'freshness_basis': None,
        }

    fee_rub = money(fee_usdt * current_rate)
    free_rub = max(Decimal('0.00'), available)
    max_request_usdt = Decimal('0.00')
    if free_rub >= fee_rub + TWOPLACES:
        max_request_usdt = usdt((free_rub - fee_rub) / current_rate)
    return {
        'rate_available': True,
        'rate_rub': current_rate,
        'fee_usdt': fee_usdt,
        'fee_rub': fee_rub,
        'available_rub': free_rub,
        'pending_rub': pending,
        'max_request_usdt': max_request_usdt,
        'rate_source': live_quote.source,
        'rate_updated_at': live_quote.updated_at,
        'rate_stale': False,
        'freshness_basis': live_quote.freshness_basis,
    }


def _assert_idempotent_match(
    existing: MerchantSettlement,
    *,
    merchant_id: UUID,
    requested_by_id: UUID,
    amount_usdt: Decimal,
    trc20_address: str,
) -> None:
    if (
        existing.merchant_id != merchant_id
        or existing.requested_by_id != requested_by_id
        or usdt(existing.amount_usdt) != amount_usdt
        or existing.trc20_address != trc20_address
    ):
        raise MerchantSettlementConflict(
            'merchant_settlement_idempotency_mismatch'
        )


async def _existing_idempotent(
    db: AsyncSession,
    *,
    merchant_id: UUID,
    idempotency_key: str,
    for_update: bool = False,
) -> MerchantSettlement | None:
    statement = select(MerchantSettlement).where(
        MerchantSettlement.merchant_id == merchant_id,
        MerchantSettlement.idempotency_key == idempotency_key,
    )
    if for_update:
        statement = statement.with_for_update()
    return (await db.execute(statement)).scalar_one_or_none()


async def create_merchant_settlement(
    db: AsyncSession,
    *,
    merchant_id: UUID,
    requested_by_id: UUID,
    amount_usdt: Decimal,
    trc20_address: str,
    idempotency_key: str,
    actor_ip: str | None = None,
) -> MerchantSettlement:
    requested_usdt = usdt(amount_usdt)
    if requested_usdt <= 0:
        raise MerchantSettlementError('invalid_usdt_amount')
    address = _validated_address(trc20_address)
    idem = _idempotency_key(idempotency_key)

    existing = await _existing_idempotent(
        db,
        merchant_id=merchant_id,
        idempotency_key=idem,
    )
    if existing:
        _assert_idempotent_match(
            existing,
            merchant_id=merchant_id,
            requested_by_id=requested_by_id,
            amount_usdt=requested_usdt,
            trc20_address=address,
        )
        return existing

    # This is the only network operation and it is completed before a row lock.
    quote = await _live_quote()
    quoted_at = datetime.now(timezone.utc)
    rate_rub = _validate_live_quote(quote, now=quoted_at)
    fee_usdt = settlement_transfer_fee_usdt()
    amount_rub = money(requested_usdt * rate_rub)
    fee_rub = money(fee_usdt * rate_rub)
    total_debit_rub = money(amount_rub + fee_rub)

    # Balance is always the first locked row for merchant settlement mutations.
    balance = await get_or_create_balance(db, merchant_id)
    existing = await _existing_idempotent(
        db,
        merchant_id=merchant_id,
        idempotency_key=idem,
        for_update=True,
    )
    if existing:
        _assert_idempotent_match(
            existing,
            merchant_id=merchant_id,
            requested_by_id=requested_by_id,
            amount_usdt=requested_usdt,
            trc20_address=address,
        )
        return existing
    pending = (
        await db.execute(
            select(MerchantSettlement.id).where(
                MerchantSettlement.merchant_id == merchant_id,
                MerchantSettlement.status == SETTLEMENT_STATUS_PENDING,
            )
        )
    ).scalar_one_or_none()
    if pending:
        raise MerchantSettlementConflict('merchant_settlement_pending')
    if total_debit_rub > money(balance.available):
        raise MerchantSettlementError(
            'merchant_settlement_insufficient_available'
        )

    snapshot = {
        'symbol': quote.symbol,
        'side': quote.side,
        'source': quote.source,
        'provider_field': quote.provider_field,
        'provider_timestamp': (
            _utc(quote.provider_timestamp).isoformat()
            if quote.provider_timestamp
            else None
        ),
        'fetched_at': _utc(quote.fetched_at).isoformat(),
        'freshness_basis': quote.freshness_basis,
    }
    settlement = MerchantSettlement(
        merchant_id=merchant_id,
        requested_by_id=requested_by_id,
        amount_usdt=requested_usdt,
        fee_usdt=fee_usdt,
        rate_rub=rate_rub,
        amount_rub=amount_rub,
        fee_rub=fee_rub,
        total_debit_rub=total_debit_rub,
        trc20_address=address,
        network=SETTLEMENT_NETWORK,
        idempotency_key=idem,
        rate_symbol=quote.symbol,
        rate_side=quote.side,
        rate_source=quote.source,
        rate_provider_field=quote.provider_field,
        provider_timestamp=quote.provider_timestamp,
        fetched_at=_utc(quote.fetched_at),
        freshness_basis=quote.freshness_basis,
        status=SETTLEMENT_STATUS_PENDING,
        metadata_json={'quote': snapshot},
    )
    db.add(settlement)
    await db.flush()
    try:
        await hold(
            db,
            merchant_id,
            total_debit_rub,
            settlement.id,
            f'merchant-settlement:{settlement.id}:hold',
            'settlement request hold',
        )
    except LedgerError as exc:
        raise MerchantSettlementError(
            'merchant_settlement_insufficient_available'
        ) from exc
    await audit(
        db,
        'merchant_settlement_requested',
        'merchant_settlement',
        requested_by_id,
        settlement.id,
        actor_ip,
        {
            'merchant_id': str(merchant_id),
            'amount_usdt': str(settlement.amount_usdt),
            'total_debit_rub': str(settlement.total_debit_rub),
            'rate_source': settlement.rate_source,
            'freshness_basis': settlement.freshness_basis,
        },
    )
    return settlement


async def _locked_settlement_and_balance(
    db: AsyncSession,
    settlement_id: UUID,
) -> tuple[MerchantSettlement, Balance]:
    merchant_id = (
        await db.execute(
            select(MerchantSettlement.merchant_id).where(
                MerchantSettlement.id == settlement_id
            )
        )
    ).scalar_one_or_none()
    if merchant_id is None:
        raise MerchantSettlementError('merchant_settlement_not_found')
    balance = await get_or_create_balance(db, merchant_id)
    settlement = (
        await db.execute(
            select(MerchantSettlement)
            .where(MerchantSettlement.id == settlement_id)
            .with_for_update()
        )
    ).scalar_one()
    if settlement.merchant_id != merchant_id:
        raise MerchantSettlementConflict('merchant_settlement_owner_changed')
    return settlement, balance


async def complete_merchant_settlement(
    db: AsyncSession,
    *,
    settlement_id: UUID,
    actor_id: UUID,
    tx_hash: str,
    actor_ip: str | None = None,
    now: datetime | None = None,
) -> MerchantSettlement:
    normalized_tx_hash = _tx_hash(tx_hash)
    settlement, _balance = await _locked_settlement_and_balance(
        db,
        settlement_id,
    )
    if settlement.status == SETTLEMENT_STATUS_COMPLETED:
        if settlement.tx_hash != normalized_tx_hash:
            raise MerchantSettlementConflict(
                'merchant_settlement_already_completed'
            )
        return settlement
    if settlement.status != SETTLEMENT_STATUS_PENDING:
        raise MerchantSettlementConflict(
            'merchant_settlement_already_rejected'
        )
    duplicate = (
        await db.execute(
            select(MerchantSettlement.id).where(
                MerchantSettlement.status == SETTLEMENT_STATUS_COMPLETED,
                MerchantSettlement.network == settlement.network,
                MerchantSettlement.tx_hash == normalized_tx_hash,
                MerchantSettlement.id != settlement.id,
            )
        )
    ).scalar_one_or_none()
    if duplicate:
        raise MerchantSettlementConflict(
            'merchant_settlement_tx_hash_duplicate'
        )
    try:
        await debit_frozen(
            db,
            settlement.merchant_id,
            settlement.total_debit_rub,
            settlement.id,
            f'merchant-settlement:{settlement.id}:debit-frozen',
            (
                f'TRC20 settle {settlement.amount_usdt} USDT '
                f'+ fee {settlement.fee_usdt} USDT'
            ),
        )
        settlement.status = SETTLEMENT_STATUS_COMPLETED
        settlement.processed_by_id = actor_id
        settlement.processed_at = _utc(now or datetime.now(timezone.utc))
        settlement.tx_hash = normalized_tx_hash
        await audit(
            db,
            'merchant_settlement_approved',
            'merchant_settlement',
            actor_id,
            settlement.id,
            actor_ip,
            {
                'merchant_id': str(settlement.merchant_id),
                'amount_usdt': str(settlement.amount_usdt),
                'total_debit_rub': str(settlement.total_debit_rub),
                'network': settlement.network,
                'tx_hash': normalized_tx_hash,
            },
        )
        await db.flush()
    except LedgerError as exc:
        raise MerchantSettlementConflict(
            'merchant_settlement_invalid_frozen_balance'
        ) from exc
    except IntegrityError as exc:
        raise MerchantSettlementConflict(
            'merchant_settlement_tx_hash_duplicate'
        ) from exc
    return settlement


async def reject_merchant_settlement(
    db: AsyncSession,
    *,
    settlement_id: UUID,
    actor_id: UUID,
    reason: str,
    actor_ip: str | None = None,
    now: datetime | None = None,
) -> MerchantSettlement:
    normalized_reason = (reason or '').strip()[:500]
    if not normalized_reason:
        raise MerchantSettlementError(
            'merchant_settlement_reject_reason_required'
        )
    settlement, _balance = await _locked_settlement_and_balance(
        db,
        settlement_id,
    )
    if settlement.status == SETTLEMENT_STATUS_REJECTED:
        return settlement
    if settlement.status != SETTLEMENT_STATUS_PENDING:
        raise MerchantSettlementConflict(
            'merchant_settlement_already_completed'
        )
    try:
        await release_hold(
            db,
            settlement.merchant_id,
            settlement.total_debit_rub,
            settlement.id,
            f'merchant-settlement:{settlement.id}:release',
            'settlement rejected',
        )
    except LedgerError as exc:
        raise MerchantSettlementConflict(
            'merchant_settlement_invalid_frozen_balance'
        ) from exc
    settlement.status = SETTLEMENT_STATUS_REJECTED
    settlement.reject_reason = normalized_reason
    settlement.processed_by_id = actor_id
    settlement.processed_at = _utc(now or datetime.now(timezone.utc))
    await audit(
        db,
        'merchant_settlement_rejected',
        'merchant_settlement',
        actor_id,
        settlement.id,
        actor_ip,
        {
            'merchant_id': str(settlement.merchant_id),
            'amount_usdt': str(settlement.amount_usdt),
            'reason': normalized_reason,
        },
    )
    await db.flush()
    return settlement
