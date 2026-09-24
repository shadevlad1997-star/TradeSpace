from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    RollingAccountStatus,
    RollingAllocationStatus,
    RollingConsumptionType,
    RollingEligibilityStatus,
    RollingLedgerType,
    RollingTransferStatus,
)
from app.models import (
    Deposit,
    Merchant,
    MerchantRollingAccount,
    MerchantRollingAllocation,
    MerchantRollingLedgerEntry,
    MerchantRollingTransfer,
    MerchantRollingTransferConsumption,
    OperationFeeSnapshot,
)
from app.services.audit import audit
from app.services.ledger import LedgerError, credit, debit_available
from app.services.rapira import RollingRapiraQuote, get_strict_rolling_ask_quote


logger = logging.getLogger('app.finance')
MONEY_QUANT = Decimal('0.01')
USDT_QUANT = Decimal('0.000001')
RATE_QUANT = Decimal('0.00000001')
PERCENT_QUANT = Decimal('0.000001')


class RollingError(ValueError):
    code = 'rolling_error'


class RollingStateConflict(RollingError):
    code = 'rolling_state_conflict'


class RollingOwnershipError(RollingError):
    code = 'rolling_transfer_forbidden'


def money(value) -> Decimal:
    try:
        result = Decimal(str(value if value is not None else '0')).quantize(
            MONEY_QUANT,
            rounding=ROUND_HALF_UP,
        )
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RollingError('invalid RUB amount') from exc
    if not result.is_finite():
        raise RollingError('invalid RUB amount')
    return result


def usdt(value) -> Decimal:
    try:
        result = Decimal(str(value if value is not None else '0')).quantize(
            USDT_QUANT,
            rounding=ROUND_HALF_UP,
        )
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RollingError('invalid USDT amount') from exc
    if not result.is_finite():
        raise RollingError('invalid USDT amount')
    return result


def rolling_rate(value) -> Decimal:
    try:
        result = Decimal(str(value)).quantize(
            RATE_QUANT,
            rounding=ROUND_HALF_UP,
        )
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RollingError('invalid Rapira rate') from exc
    if not result.is_finite() or result <= 0:
        raise RollingError('invalid Rapira rate')
    return result


def percentage(value) -> Decimal:
    try:
        result = Decimal(str(value)).quantize(
            PERCENT_QUANT,
            rounding=ROUND_HALF_UP,
        )
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RollingError('invalid percentage') from exc
    if not result.is_finite() or result < 0 or result > 100:
        raise RollingError('invalid percentage')
    return result


def aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def get_rolling_account(
    db: AsyncSession,
    merchant_id: UUID,
    *,
    lock: bool = False,
) -> MerchantRollingAccount | None:
    query = select(MerchantRollingAccount).where(
        MerchantRollingAccount.merchant_id == merchant_id
    )
    if lock:
        query = query.with_for_update()
    return (await db.execute(query)).scalar_one_or_none()


async def get_or_create_rolling_account(
    db: AsyncSession,
    merchant_id: UUID,
    *,
    lock: bool = True,
) -> MerchantRollingAccount:
    account = await get_rolling_account(db, merchant_id, lock=lock)
    if account:
        return account
    # The account does not exist yet, so there is no account row to lock.
    # Lock the merchant as the stable per-merchant creation mutex, then
    # re-check before inserting. This keeps concurrent confirmations from
    # racing the unique merchant/account constraint.
    merchant_exists = (
        await db.execute(
            select(Merchant.id)
            .where(Merchant.id == merchant_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if merchant_exists is None:
        raise RollingError('merchant is missing')
    account = await get_rolling_account(db, merchant_id, lock=lock)
    if account:
        return account
    account = MerchantRollingAccount(
        merchant_id=merchant_id,
        principal_usdt=Decimal('0.000000'),
        recovered_usdt=Decimal('0.000000'),
        outstanding_usdt=Decimal('0.000000'),
        status=RollingAccountStatus.exhausted.value,
    )
    db.add(account)
    await db.flush()
    return account


async def list_rolling_transfers(
    db: AsyncSession,
    merchant_id: UUID,
    *,
    limit: int = 200,
) -> list[MerchantRollingTransfer]:
    return list(
        (
            await db.execute(
                select(MerchantRollingTransfer)
                .where(MerchantRollingTransfer.merchant_id == merchant_id)
                .order_by(
                    MerchantRollingTransfer.sequence_no.desc(),
                    MerchantRollingTransfer.created_at.desc(),
                )
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


def _validate_transfer_evidence(
    *,
    amount_usdt: Decimal,
    network: str,
    destination_address: str,
    tx_hash: str,
    sent_at: datetime,
    idempotency_key: str,
) -> tuple[Decimal, str, str, str, datetime, str]:
    amount = usdt(amount_usdt)
    normalized_network = (network or '').strip().upper()
    destination = (destination_address or '').strip()
    normalized_hash = (tx_hash or '').strip()
    normalized_key = (idempotency_key or '').strip()
    if amount <= 0:
        raise RollingError('transfer amount must be positive')
    if not normalized_network or not destination or not normalized_hash:
        raise RollingError('network, destination address, and tx hash are required')
    if not normalized_key:
        raise RollingError('idempotency key is required')
    if len(normalized_network) > 32:
        raise RollingError('network is too long')
    if len(destination) > 160 or len(normalized_hash) > 160:
        raise RollingError('transfer evidence is too long')
    if len(normalized_key) > 180:
        raise RollingError('idempotency key is too long')
    return (
        amount,
        normalized_network,
        destination,
        normalized_hash,
        aware_utc(sent_at),
        normalized_key,
    )


def _transfer_request_matches(
    transfer: MerchantRollingTransfer,
    *,
    merchant_id: UUID,
    amount_usdt: Decimal,
    network: str,
    destination_address: str,
    tx_hash: str,
    sent_at: datetime,
) -> bool:
    return (
        transfer.merchant_id == merchant_id
        and usdt(transfer.amount_usdt) == amount_usdt
        and transfer.network == network
        and transfer.destination_address == destination_address
        and transfer.tx_hash == tx_hash
        and aware_utc(transfer.sent_at) == sent_at
    )


async def register_rolling_transfer(
    db: AsyncSession,
    *,
    merchant_id: UUID,
    actor_id: UUID,
    amount_usdt: Decimal,
    network: str,
    destination_address: str,
    tx_hash: str,
    sent_at: datetime,
    comment: str | None,
    idempotency_key: str,
) -> MerchantRollingTransfer:
    (
        amount,
        normalized_network,
        destination,
        normalized_hash,
        sent_at_utc,
        normalized_key,
    ) = _validate_transfer_evidence(
        amount_usdt=amount_usdt,
        network=network,
        destination_address=destination_address,
        tx_hash=tx_hash,
        sent_at=sent_at,
        idempotency_key=idempotency_key,
    )
    existing = (
        await db.execute(
            select(MerchantRollingTransfer).where(
                MerchantRollingTransfer.idempotency_key == normalized_key
            )
        )
    ).scalar_one_or_none()
    if existing:
        if not _transfer_request_matches(
            existing,
            merchant_id=merchant_id,
            amount_usdt=amount,
            network=normalized_network,
            destination_address=destination,
            tx_hash=normalized_hash,
            sent_at=sent_at_utc,
        ):
            raise RollingStateConflict('transfer idempotency conflict')
        return existing

    # Serialize sequence allocation without creating or changing an account.
    merchant_exists = (
        await db.execute(
            select(Merchant.id)
            .where(Merchant.id == merchant_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if merchant_exists is None:
        raise RollingError('merchant is missing')
    # A concurrent retry may have committed while this transaction waited for
    # the merchant mutex. Re-check both idempotency and transfer evidence.
    existing = (
        await db.execute(
            select(MerchantRollingTransfer).where(
                MerchantRollingTransfer.idempotency_key == normalized_key
            )
        )
    ).scalar_one_or_none()
    if existing:
        if not _transfer_request_matches(
            existing,
            merchant_id=merchant_id,
            amount_usdt=amount,
            network=normalized_network,
            destination_address=destination,
            tx_hash=normalized_hash,
            sent_at=sent_at_utc,
        ):
            raise RollingStateConflict('transfer idempotency conflict')
        return existing
    duplicate_evidence = (
        await db.execute(
            select(MerchantRollingTransfer.id).where(
                MerchantRollingTransfer.network == normalized_network,
                MerchantRollingTransfer.tx_hash == normalized_hash,
            )
        )
    ).scalar_one_or_none()
    if duplicate_evidence is not None:
        raise RollingStateConflict(
            'transfer with this network and tx hash is already registered'
        )
    sequence_no = int(
        (
            await db.execute(
                select(
                    func.coalesce(
                        func.max(MerchantRollingTransfer.sequence_no),
                        0,
                    )
                ).where(MerchantRollingTransfer.merchant_id == merchant_id)
            )
        ).scalar_one()
    ) + 1
    transfer = MerchantRollingTransfer(
        merchant_id=merchant_id,
        rolling_account_id=None,
        sequence_no=sequence_no,
        amount_usdt=amount,
        recovered_usdt=Decimal('0.000000'),
        remaining_usdt=Decimal('0.000000'),
        network=normalized_network,
        destination_address=destination,
        tx_hash=normalized_hash,
        status=RollingTransferStatus.pending_confirmation.value,
        source='registered',
        sent_at=sent_at_utc,
        created_by=actor_id,
        comment=(comment or '').strip()[:500] or None,
        idempotency_key=normalized_key,
    )
    db.add(transfer)
    await db.flush()
    await audit(
        db,
        'rolling_transfer_registered',
        'merchant_rolling_transfer',
        actor_id,
        transfer.id,
        details={
            'merchant_id': str(merchant_id),
            'sequence_no': sequence_no,
            'amount_usdt': str(amount),
            'network': normalized_network,
        },
    )
    return transfer


async def confirm_rolling_transfer(
    db: AsyncSession,
    *,
    transfer_id: UUID,
    merchant_id: UUID,
    actor_id: UUID,
    confirmed_at: datetime | None = None,
) -> MerchantRollingTransfer:
    transfer = (
        await db.execute(
            select(MerchantRollingTransfer)
            .where(MerchantRollingTransfer.id == transfer_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not transfer:
        raise RollingError('rolling transfer is missing')
    if transfer.merchant_id != merchant_id:
        raise RollingOwnershipError('rolling transfer does not belong to merchant')
    if transfer.status == RollingTransferStatus.confirmed.value:
        return transfer
    if transfer.status != RollingTransferStatus.pending_confirmation.value:
        raise RollingStateConflict(
            f'rolling transfer in {transfer.status} cannot be confirmed'
        )

    account = await get_or_create_rolling_account(db, merchant_id, lock=True)
    amount = usdt(transfer.amount_usdt)
    entry_type = (
        RollingLedgerType.funding.value
        if usdt(account.principal_usdt) == 0
        else RollingLedgerType.topup.value
    )
    confirmation_time = aware_utc(
        confirmed_at or datetime.now(timezone.utc)
    )
    account.principal_usdt = usdt(account.principal_usdt + amount)
    account.outstanding_usdt = usdt(account.outstanding_usdt + amount)
    if account.status != RollingAccountStatus.suspended.value:
        account.status = RollingAccountStatus.active.value

    transfer.rolling_account_id = account.id
    transfer.recovered_usdt = Decimal('0.000000')
    transfer.remaining_usdt = amount
    transfer.status = RollingTransferStatus.confirmed.value
    transfer.confirmed_at = confirmation_time
    transfer.confirmed_by = actor_id
    ledger_entry = MerchantRollingLedgerEntry(
        rolling_account_id=account.id,
        rolling_transfer_id=transfer.id,
        merchant_id=merchant_id,
        actor_id=actor_id,
        entry_type=entry_type,
        amount_usdt=amount,
        network=transfer.network,
        destination_address=transfer.destination_address,
        tx_hash=transfer.tx_hash,
        funded_at=transfer.sent_at,
        reason='merchant confirmed Rolling transfer receipt',
        idempotency_key=f'rolling-transfer:{transfer.id}:confirmed',
        metadata_json={
            'transfer_sequence': transfer.sequence_no,
            'confirmed_at': confirmation_time.isoformat(),
        },
    )
    db.add(ledger_entry)
    await db.flush()
    await audit(
        db,
        'rolling_transfer_confirmed',
        'merchant_rolling_transfer',
        actor_id,
        transfer.id,
        details={
            'merchant_id': str(merchant_id),
            'sequence_no': transfer.sequence_no,
            'amount_usdt': str(amount),
            'rolling_account_id': str(account.id),
        },
    )
    return transfer


async def dispute_rolling_transfer(
    db: AsyncSession,
    *,
    transfer_id: UUID,
    merchant_id: UUID,
    actor_id: UUID,
    reason: str,
) -> MerchantRollingTransfer:
    normalized_reason = (reason or '').strip()
    if not normalized_reason:
        raise RollingError('dispute reason is required')
    transfer = (
        await db.execute(
            select(MerchantRollingTransfer)
            .where(MerchantRollingTransfer.id == transfer_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not transfer:
        raise RollingError('rolling transfer is missing')
    if transfer.merchant_id != merchant_id:
        raise RollingOwnershipError('rolling transfer does not belong to merchant')
    if transfer.status == RollingTransferStatus.disputed.value:
        return transfer
    if transfer.status != RollingTransferStatus.pending_confirmation.value:
        raise RollingStateConflict(
            f'rolling transfer in {transfer.status} cannot be disputed'
        )
    transfer.status = RollingTransferStatus.disputed.value
    transfer.disputed_at = datetime.now(timezone.utc)
    transfer.disputed_by = actor_id
    transfer.dispute_reason = normalized_reason[:500]
    await audit(
        db,
        'rolling_transfer_disputed',
        'merchant_rolling_transfer',
        actor_id,
        transfer.id,
        details={
            'merchant_id': str(merchant_id),
            'sequence_no': transfer.sequence_no,
            'reason': transfer.dispute_reason,
        },
    )
    return transfer


async def cancel_rolling_transfer(
    db: AsyncSession,
    *,
    transfer_id: UUID,
    actor_id: UUID,
    reason: str,
) -> MerchantRollingTransfer:
    normalized_reason = (reason or '').strip()
    if not normalized_reason:
        raise RollingError('cancel reason is required')
    transfer = (
        await db.execute(
            select(MerchantRollingTransfer)
            .where(MerchantRollingTransfer.id == transfer_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not transfer:
        raise RollingError('rolling transfer is missing')
    if transfer.status == RollingTransferStatus.cancelled.value:
        return transfer
    if transfer.status not in {
        RollingTransferStatus.pending_confirmation.value,
        RollingTransferStatus.disputed.value,
    }:
        raise RollingStateConflict(
            'confirmed Rolling transfer requires a compensating financial operation'
        )
    transfer.status = RollingTransferStatus.cancelled.value
    transfer.cancelled_at = datetime.now(timezone.utc)
    transfer.cancelled_by = actor_id
    transfer.cancel_reason = normalized_reason[:500]
    await audit(
        db,
        'rolling_transfer_cancelled',
        'merchant_rolling_transfer',
        actor_id,
        transfer.id,
        details={
            'merchant_id': str(transfer.merchant_id),
            'sequence_no': transfer.sequence_no,
            'reason': transfer.cancel_reason,
        },
    )
    return transfer


@dataclass(frozen=True)
class RollingDepositQuote:
    quote: RollingRapiraQuote
    eligible_transfer_sequence: int
    rolling_eligible_at: datetime


async def _eligible_transfer_sequence(
    db: AsyncSession,
    merchant_id: UUID,
    created_at: datetime,
) -> int | None:
    account = await get_rolling_account(db, merchant_id)
    if (
        not account
        or account.status != RollingAccountStatus.active.value
        or usdt(account.outstanding_usdt) <= 0
    ):
        return None
    return (
        await db.execute(
            select(func.max(MerchantRollingTransfer.sequence_no)).where(
                MerchantRollingTransfer.merchant_id == merchant_id,
                MerchantRollingTransfer.status
                == RollingTransferStatus.confirmed.value,
                MerchantRollingTransfer.remaining_usdt > 0,
                MerchantRollingTransfer.confirmed_at <= aware_utc(created_at),
            )
        )
    ).scalar_one_or_none()


async def rolling_quote_for_deposit_create(
    db: AsyncSession,
    merchant_id: UUID,
    *,
    created_at: datetime | None = None,
) -> RollingDepositQuote | None:
    deposit_created_at = aware_utc(created_at or datetime.now(timezone.utc))
    sequence_no = await _eligible_transfer_sequence(
        db,
        merchant_id,
        deposit_created_at,
    )
    if sequence_no is None:
        return None
    # Deliberately outside any SELECT ... FOR UPDATE section.
    quote = await get_strict_rolling_ask_quote()
    return RollingDepositQuote(
        quote=quote,
        eligible_transfer_sequence=int(sequence_no),
        rolling_eligible_at=deposit_created_at,
    )


async def create_pending_allocation(
    db: AsyncSession,
    *,
    deposit: Deposit,
    snapshot: OperationFeeSnapshot,
    quote: RollingDepositQuote | None,
) -> MerchantRollingAllocation | None:
    if quote is None:
        return None
    existing = (
        await db.execute(
            select(MerchantRollingAllocation).where(
                MerchantRollingAllocation.deposit_id == deposit.id
            )
        )
    ).scalar_one_or_none()
    if existing:
        return existing
    deposit_created_at = aware_utc(deposit.created_at)
    if deposit_created_at != aware_utc(quote.rolling_eligible_at):
        raise RollingStateConflict('Rolling eligibility timestamp mismatch')
    eligible_transfer = (
        await db.execute(
            select(MerchantRollingTransfer).where(
                MerchantRollingTransfer.merchant_id == deposit.merchant_id,
                MerchantRollingTransfer.status
                == RollingTransferStatus.confirmed.value,
                MerchantRollingTransfer.sequence_no
                == quote.eligible_transfer_sequence,
                MerchantRollingTransfer.confirmed_at <= deposit_created_at,
            )
        )
    ).scalar_one_or_none()
    if not eligible_transfer or not eligible_transfer.rolling_account_id:
        raise RollingStateConflict('confirmed Rolling eligibility disappeared')

    gross = money(snapshot.calculation_base_amount)
    merchant_fee = money(snapshot.merchant_fee_amount)
    merchant_payable = money(gross - merchant_fee)
    if merchant_payable < 0:
        raise RollingError('merchant payable cannot be negative')
    rate_value = rolling_rate(quote.quote.rate)
    merchant_payable_usdt = usdt(merchant_payable / rate_value)
    allocation = MerchantRollingAllocation(
        deposit_id=deposit.id,
        merchant_id=deposit.merchant_id,
        rolling_account_id=eligible_transfer.rolling_account_id,
        gross_rub=gross,
        merchant_fee_percent_snapshot=percentage(
            snapshot.merchant_rate_percent
        ),
        merchant_fee_rub=merchant_fee,
        merchant_payable_rub=merchant_payable,
        rapira_rate_rub=rate_value,
        rapira_rate_symbol=quote.quote.symbol,
        rapira_rate_side=quote.quote.side,
        rapira_rate_source=quote.quote.source,
        rapira_rate_field=quote.quote.provider_field,
        rapira_rate_updated_at=quote.quote.updated_at,
        rapira_provider_timestamp=quote.quote.provider_timestamp,
        rapira_fetched_at=quote.quote.fetched_at,
        rapira_freshness_basis=quote.quote.freshness_basis,
        merchant_payable_usdt=merchant_payable_usdt,
        eligibility_status=RollingEligibilityStatus.eligible.value,
        eligible_transfer_sequence=quote.eligible_transfer_sequence,
        rolling_eligible_at=deposit_created_at,
        eligibility_source='confirmed_transfer',
        status=RollingAllocationStatus.pending.value,
    )
    db.add(allocation)
    await db.flush()
    db.add(
        MerchantRollingLedgerEntry(
            rolling_account_id=eligible_transfer.rolling_account_id,
            merchant_id=deposit.merchant_id,
            deposit_id=deposit.id,
            entry_type=RollingLedgerType.pending_added.value,
            amount_usdt=merchant_payable_usdt,
            amount_rub=merchant_payable,
            rate_rub=rate_value,
            reason='Rolling pending exposure created',
            idempotency_key=f'rolling:{deposit.id}:pending-added',
            metadata_json={
                'rapira_side': quote.quote.side,
                'rapira_source': quote.quote.source,
                'rapira_field': quote.quote.provider_field,
                'rapira_symbol': quote.quote.symbol,
                'rapira_provider_timestamp': (
                    quote.quote.provider_timestamp.isoformat()
                    if quote.quote.provider_timestamp
                    else None
                ),
                'rapira_fetched_at': quote.quote.fetched_at.isoformat(),
                'rapira_freshness_basis': quote.quote.freshness_basis,
                'eligible_transfer_sequence': quote.eligible_transfer_sequence,
                'rolling_eligible_at': deposit_created_at.isoformat(),
                'eligibility_status': RollingEligibilityStatus.eligible.value,
            },
        )
    )
    return allocation


def _settle_result(merchant_payable_rub: Decimal) -> dict:
    return {
        'financing_route': 'settle',
        'has_confirmed_rolling': False,
        'rolling_applied_usdt': Decimal('0.000000'),
        'rolling_applied_rub': Decimal('0.00'),
        'settle_credited_rub': money(merchant_payable_rub),
        'rapira_rate_rub': None,
        'rapira_rate_side': None,
        'rapira_rate_source': None,
    }


async def apply_merchant_financing(
    db: AsyncSession,
    *,
    deposit: Deposit,
    snapshot: OperationFeeSnapshot,
    merchant_payable_rub: Decimal,
    description: str,
) -> dict:
    merchant_payable_rub = money(merchant_payable_rub)
    locked_deposit = (
        await db.execute(
            select(Deposit)
            .where(Deposit.id == deposit.id)
            .with_for_update()
        )
    ).scalar_one()
    if locked_deposit.merchant_id != deposit.merchant_id:
        raise RollingError('deposit merchant mismatch')

    allocation = (
        await db.execute(
            select(MerchantRollingAllocation)
            .where(MerchantRollingAllocation.deposit_id == deposit.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not allocation:
        if merchant_payable_rub > 0:
            await credit(
                db,
                deposit.merchant_id,
                merchant_payable_rub,
                deposit.id,
                f'deposit-confirm:{deposit.id}:payable',
                description,
            )
        return _settle_result(merchant_payable_rub)

    if allocation.status == RollingAllocationStatus.paid.value:
        return _allocation_financing_result(allocation)
    if allocation.status != RollingAllocationStatus.pending.value:
        raise RollingError(
            f'Rolling allocation status {allocation.status} cannot be paid'
        )
    if money(allocation.merchant_payable_rub) != merchant_payable_rub:
        raise RollingError(
            'Rolling allocation merchant payable does not match fee snapshot'
        )
    if (
        allocation.eligibility_status
        == RollingEligibilityStatus.ineligible.value
    ):
        if (
            allocation.eligible_transfer_sequence is not None
            or allocation.rolling_eligible_at is not None
            or usdt(allocation.rolling_applied_usdt) != 0
            or money(allocation.rolling_applied_rub) != 0
        ):
            raise RollingError('invalid ineligible Rolling allocation state')
        if merchant_payable_rub > 0:
            await credit(
                db,
                deposit.merchant_id,
                merchant_payable_rub,
                deposit.id,
                f'deposit-confirm:{deposit.id}:payable',
                description,
            )
        allocation.settle_credited_rub = merchant_payable_rub
        allocation.status = RollingAllocationStatus.paid.value
        allocation.finalized_at = datetime.now(timezone.utc)
        db.add(
            MerchantRollingLedgerEntry(
                rolling_account_id=allocation.rolling_account_id,
                merchant_id=deposit.merchant_id,
                deposit_id=deposit.id,
                entry_type=RollingLedgerType.pending_released.value,
                amount_usdt=allocation.merchant_payable_usdt,
                amount_rub=merchant_payable_rub,
                rate_rub=allocation.rapira_rate_rub,
                reason='Legacy ineligible Rolling exposure finalized to settle',
                idempotency_key=(
                    f'rolling:{deposit.id}:pending-released-paid'
                ),
            )
        )
        if merchant_payable_rub > 0:
            db.add(
                MerchantRollingLedgerEntry(
                    rolling_account_id=allocation.rolling_account_id,
                    merchant_id=deposit.merchant_id,
                    deposit_id=deposit.id,
                    entry_type=RollingLedgerType.settle_overflow.value,
                    amount_usdt=allocation.merchant_payable_usdt,
                    amount_rub=merchant_payable_rub,
                    rate_rub=allocation.rapira_rate_rub,
                    reason='Legacy ineligible Rolling payable credited to settle',
                    idempotency_key=f'rolling:{deposit.id}:settle-overflow',
                )
            )
        return _allocation_financing_result(allocation)
    if (
        allocation.eligibility_status
        != RollingEligibilityStatus.eligible.value
        or allocation.eligible_transfer_sequence is None
        or allocation.rolling_eligible_at is None
    ):
        raise RollingError('invalid eligible Rolling allocation state')

    account = (
        await db.execute(
            select(MerchantRollingAccount)
            .where(
                MerchantRollingAccount.id == allocation.rolling_account_id
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not account:
        raise RollingError('Rolling account is missing')

    transfers = list(
        (
            await db.execute(
                select(MerchantRollingTransfer)
                .where(
                    MerchantRollingTransfer.rolling_account_id == account.id,
                    MerchantRollingTransfer.status
                    == RollingTransferStatus.confirmed.value,
                    MerchantRollingTransfer.sequence_no
                    <= allocation.eligible_transfer_sequence,
                    MerchantRollingTransfer.confirmed_at
                    <= allocation.rolling_eligible_at,
                    MerchantRollingTransfer.remaining_usdt > 0,
                )
                .order_by(
                    MerchantRollingTransfer.sequence_no.asc(),
                    MerchantRollingTransfer.id.asc(),
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    merchant_payable_usdt = usdt(allocation.merchant_payable_usdt)
    eligible_remaining = usdt(
        sum((usdt(row.remaining_usdt) for row in transfers), Decimal('0'))
    )
    applied_usdt = min(merchant_payable_usdt, eligible_remaining)
    if applied_usdt == merchant_payable_usdt:
        applied_rub = merchant_payable_rub
    else:
        applied_rub = min(
            merchant_payable_rub,
            money(applied_usdt * rolling_rate(allocation.rapira_rate_rub)),
        )
    settle_credit = money(merchant_payable_rub - applied_rub)
    if money(applied_rub + settle_credit) != merchant_payable_rub:
        raise RollingError('Rolling RUB split invariant failed')

    usdt_left = applied_usdt
    rub_left = applied_rub
    consumed: list[tuple[MerchantRollingTransfer, Decimal, Decimal]] = []
    for transfer in transfers:
        if usdt_left <= 0:
            break
        consumed_usdt = min(usdt(transfer.remaining_usdt), usdt_left)
        if consumed_usdt <= 0:
            continue
        if consumed_usdt == usdt_left:
            consumed_rub = rub_left
        else:
            consumed_rub = min(
                rub_left,
                money(
                    consumed_usdt
                    * rolling_rate(allocation.rapira_rate_rub)
                ),
            )
        transfer.recovered_usdt = usdt(
            transfer.recovered_usdt + consumed_usdt
        )
        transfer.remaining_usdt = usdt(
            transfer.remaining_usdt - consumed_usdt
        )
        if (
            transfer.remaining_usdt < 0
            or usdt(transfer.recovered_usdt + transfer.remaining_usdt)
            != usdt(transfer.amount_usdt)
        ):
            raise RollingError('Rolling transfer invariant failed')
        consumed.append((transfer, consumed_usdt, consumed_rub))
        usdt_left = usdt(usdt_left - consumed_usdt)
        rub_left = money(rub_left - consumed_rub)
    if usdt_left != 0 or rub_left != 0:
        raise RollingError('Rolling FIFO consumption split failed')

    for transfer, consumed_usdt, consumed_rub in consumed:
        db.add(
            MerchantRollingTransferConsumption(
                merchant_id=deposit.merchant_id,
                rolling_account_id=account.id,
                rolling_transfer_id=transfer.id,
                rolling_allocation_id=allocation.id,
                deposit_id=deposit.id,
                entry_type=RollingConsumptionType.recovery.value,
                amount_usdt=consumed_usdt,
                amount_rub=consumed_rub,
                rate_rub=allocation.rapira_rate_rub,
                idempotency_key=(
                    f'rolling-consumption:{transfer.id}:{deposit.id}:recovery'
                ),
                reason='successful deposit Rolling recovery',
            )
        )

    account.recovered_usdt = usdt(account.recovered_usdt + applied_usdt)
    account.outstanding_usdt = usdt(
        account.outstanding_usdt - applied_usdt
    )
    if account.outstanding_usdt < 0:
        raise RollingError('Rolling outstanding cannot be negative')
    if account.outstanding_usdt == 0:
        account.status = RollingAccountStatus.exhausted.value
    elif account.status != RollingAccountStatus.suspended.value:
        account.status = RollingAccountStatus.active.value

    allocation.rolling_applied_usdt = applied_usdt
    allocation.rolling_applied_rub = applied_rub
    allocation.settle_credited_rub = settle_credit
    allocation.status = RollingAllocationStatus.paid.value
    allocation.finalized_at = datetime.now(timezone.utc)
    db.add(
        MerchantRollingLedgerEntry(
            rolling_account_id=account.id,
            merchant_id=deposit.merchant_id,
            deposit_id=deposit.id,
            entry_type=RollingLedgerType.pending_released.value,
            amount_usdt=merchant_payable_usdt,
            amount_rub=merchant_payable_rub,
            rate_rub=allocation.rapira_rate_rub,
            reason='Rolling pending exposure finalized as paid',
            idempotency_key=f'rolling:{deposit.id}:pending-released-paid',
        )
    )
    if applied_usdt > 0:
        db.add(
            MerchantRollingLedgerEntry(
                rolling_account_id=account.id,
                merchant_id=deposit.merchant_id,
                deposit_id=deposit.id,
                rolling_transfer_id=(
                    consumed[0][0].id if len(consumed) == 1 else None
                ),
                entry_type=RollingLedgerType.recovery.value,
                amount_usdt=applied_usdt,
                amount_rub=applied_rub,
                rate_rub=allocation.rapira_rate_rub,
                reason='successful deposit Rolling recovery',
                idempotency_key=f'rolling:{deposit.id}:recovery',
                metadata_json={
                    'transfer_sequences': [
                        row.sequence_no for row, _, _ in consumed
                    ],
                },
            )
        )
    if settle_credit > 0:
        await credit(
            db,
            deposit.merchant_id,
            settle_credit,
            deposit.id,
            f'deposit-confirm:{deposit.id}:payable',
            f'{description} (Rolling overflow)',
        )
        db.add(
            MerchantRollingLedgerEntry(
                rolling_account_id=account.id,
                merchant_id=deposit.merchant_id,
                deposit_id=deposit.id,
                entry_type=RollingLedgerType.settle_overflow.value,
                amount_usdt=usdt(
                    settle_credit
                    / rolling_rate(allocation.rapira_rate_rub)
                ),
                amount_rub=settle_credit,
                rate_rub=allocation.rapira_rate_rub,
                reason='successful deposit settle overflow',
                idempotency_key=f'rolling:{deposit.id}:settle-overflow',
            )
        )
    return _allocation_financing_result(allocation)


def _allocation_financing_result(
    allocation: MerchantRollingAllocation,
) -> dict:
    is_eligible = (
        allocation.eligibility_status
        == RollingEligibilityStatus.eligible.value
    )
    return {
        'financing_route': (
            (
                'rolling'
                if money(allocation.settle_credited_rub) == 0
                else 'rolling_and_settle'
            )
            if is_eligible
            else 'settle'
        ),
        'has_confirmed_rolling': is_eligible,
        'rolling_eligible': is_eligible,
        'rolling_applied_usdt': usdt(allocation.rolling_applied_usdt),
        'rolling_applied_rub': money(allocation.rolling_applied_rub),
        'settle_credited_rub': money(allocation.settle_credited_rub),
        'rapira_rate_rub': rolling_rate(allocation.rapira_rate_rub),
        'rapira_rate_symbol': allocation.rapira_rate_symbol,
        'rapira_rate_side': allocation.rapira_rate_side,
        'rapira_rate_source': allocation.rapira_rate_source,
        'rapira_provider_timestamp': allocation.rapira_provider_timestamp,
        'rapira_fetched_at': allocation.rapira_fetched_at,
        'rapira_freshness_basis': allocation.rapira_freshness_basis,
        'eligibility_status': allocation.eligibility_status,
        'eligibility_source': allocation.eligibility_source,
        'eligible_transfer_sequence': allocation.eligible_transfer_sequence,
        'rolling_eligible_at': allocation.rolling_eligible_at,
    }


async def release_pending_allocation(
    db: AsyncSession,
    deposit: Deposit,
    *,
    reason: str,
) -> MerchantRollingAllocation | None:
    await db.execute(
        select(Deposit).where(Deposit.id == deposit.id).with_for_update()
    )
    allocation = (
        await db.execute(
            select(MerchantRollingAllocation)
            .where(MerchantRollingAllocation.deposit_id == deposit.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not allocation:
        return None
    if allocation.status != RollingAllocationStatus.pending.value:
        return allocation
    account = (
        await db.execute(
            select(MerchantRollingAccount)
            .where(
                MerchantRollingAccount.id == allocation.rolling_account_id
            )
            .with_for_update()
        )
    ).scalar_one()
    allocation.status = RollingAllocationStatus.released.value
    allocation.release_reason = (reason or 'failed')[:64]
    allocation.finalized_at = datetime.now(timezone.utc)
    db.add(
        MerchantRollingLedgerEntry(
            rolling_account_id=account.id,
            merchant_id=deposit.merchant_id,
            deposit_id=deposit.id,
            entry_type=RollingLedgerType.pending_released.value,
            amount_usdt=allocation.merchant_payable_usdt,
            amount_rub=allocation.merchant_payable_rub,
            rate_rub=allocation.rapira_rate_rub,
            reason=(
                'Rolling pending exposure released: '
                f'{allocation.release_reason}'
            ),
            idempotency_key=f'rolling:{deposit.id}:pending-released',
        )
    )
    return allocation


async def reopen_released_allocation(
    db: AsyncSession,
    deposit: Deposit,
    *,
    reason: str,
    idempotency_suffix: str,
) -> MerchantRollingAllocation | None:
    await db.execute(
        select(Deposit).where(Deposit.id == deposit.id).with_for_update()
    )
    allocation = (
        await db.execute(
            select(MerchantRollingAllocation)
            .where(MerchantRollingAllocation.deposit_id == deposit.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not allocation:
        return None
    if allocation.status == RollingAllocationStatus.pending.value:
        return allocation
    if allocation.status != RollingAllocationStatus.released.value:
        raise RollingError(
            f'Rolling allocation status {allocation.status} cannot be reopened'
        )
    account = (
        await db.execute(
            select(MerchantRollingAccount)
            .where(
                MerchantRollingAccount.id == allocation.rolling_account_id
            )
            .with_for_update()
        )
    ).scalar_one()
    allocation.status = RollingAllocationStatus.pending.value
    allocation.release_reason = None
    allocation.finalized_at = None
    db.add(
        MerchantRollingLedgerEntry(
            rolling_account_id=account.id,
            merchant_id=deposit.merchant_id,
            deposit_id=deposit.id,
            entry_type=RollingLedgerType.pending_added.value,
            amount_usdt=allocation.merchant_payable_usdt,
            amount_rub=allocation.merchant_payable_rub,
            rate_rub=allocation.rapira_rate_rub,
            reason=f'Rolling pending exposure reopened: {reason}'[:500],
            idempotency_key=(
                f'rolling:{deposit.id}:pending-reopened:{idempotency_suffix}'
            )[:180],
        )
    )
    return allocation


async def set_rolling_suspension(
    db: AsyncSession,
    *,
    merchant_id: UUID,
    suspended: bool,
    actor_id: UUID,
    reason: str,
) -> MerchantRollingAccount:
    normalized_reason = (reason or '').strip()
    if not normalized_reason:
        raise RollingError('reason is required')
    account = await get_rolling_account(db, merchant_id, lock=True)
    if not account:
        raise RollingError('Rolling account is missing')
    account.status = (
        RollingAccountStatus.suspended.value
        if suspended
        else (
            RollingAccountStatus.active.value
            if usdt(account.outstanding_usdt) > 0
            else RollingAccountStatus.exhausted.value
        )
    )
    db.add(
        MerchantRollingLedgerEntry(
            rolling_account_id=account.id,
            merchant_id=merchant_id,
            actor_id=actor_id,
            entry_type=RollingLedgerType.manual_adjustment.value,
            amount_usdt=Decimal('0.000000'),
            reason=(
                f'Rolling {"suspended" if suspended else "resumed"}: '
                f'{normalized_reason}'
            )[:500],
            idempotency_key=(
                f'rolling:{account.id}:'
                f'{"suspend" if suspended else "resume"}:'
                f'{datetime.now(timezone.utc).isoformat()}'
            ),
        )
    )
    return account


async def reverse_paid_allocation(
    db: AsyncSession,
    *,
    deposit: Deposit,
    actor_id: UUID | None,
    reason: str,
) -> MerchantRollingAllocation | None:
    normalized_reason = (reason or '').strip()
    if not normalized_reason:
        raise RollingError('reversal reason is required')
    await db.execute(
        select(Deposit).where(Deposit.id == deposit.id).with_for_update()
    )
    allocation = (
        await db.execute(
            select(MerchantRollingAllocation)
            .where(MerchantRollingAllocation.deposit_id == deposit.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not allocation:
        return None
    if allocation.status == RollingAllocationStatus.reversed.value:
        return allocation
    if allocation.status != RollingAllocationStatus.paid.value:
        raise RollingError('only paid Rolling allocation can be reversed')
    account = (
        await db.execute(
            select(MerchantRollingAccount)
            .where(
                MerchantRollingAccount.id == allocation.rolling_account_id
            )
            .with_for_update()
        )
    ).scalar_one()
    recovery_rows = list(
        (
            await db.execute(
                select(MerchantRollingTransferConsumption).where(
                    MerchantRollingTransferConsumption.rolling_allocation_id
                    == allocation.id,
                    MerchantRollingTransferConsumption.entry_type
                    == RollingConsumptionType.recovery.value,
                )
            )
        )
        .scalars()
        .all()
    )
    transfer_ids = [row.rolling_transfer_id for row in recovery_rows]
    transfers = list(
        (
            await db.execute(
                select(MerchantRollingTransfer)
                .where(MerchantRollingTransfer.id.in_(transfer_ids))
                .order_by(
                    MerchantRollingTransfer.sequence_no.asc(),
                    MerchantRollingTransfer.id.asc(),
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    transfers_by_id = {row.id: row for row in transfers}
    applied_usdt = usdt(allocation.rolling_applied_usdt)
    if usdt(account.recovered_usdt) < applied_usdt:
        raise RollingError('Rolling recovered balance is lower than reversal')
    if applied_usdt > 0 and usdt(
        sum((usdt(row.amount_usdt) for row in recovery_rows), Decimal('0'))
    ) != applied_usdt:
        raise RollingError('Rolling consumption evidence is incomplete')

    settle_credit = money(allocation.settle_credited_rub)
    if settle_credit > 0:
        try:
            await debit_available(
                db,
                deposit.merchant_id,
                settle_credit,
                deposit.id,
                f'rolling:{deposit.id}:settle-reversal',
                f'Rolling reversal: {normalized_reason}',
            )
        except LedgerError as exc:
            raise RollingError(
                'settle credit is unavailable; receivable workflow is required'
            ) from exc

    for recovery in recovery_rows:
        transfer = transfers_by_id.get(recovery.rolling_transfer_id)
        if not transfer:
            raise RollingError('Rolling transfer evidence is missing')
        amount = usdt(recovery.amount_usdt)
        if usdt(transfer.recovered_usdt) < amount:
            raise RollingError('Rolling transfer recovered is too low')
        transfer.recovered_usdt = usdt(transfer.recovered_usdt - amount)
        transfer.remaining_usdt = usdt(transfer.remaining_usdt + amount)
        db.add(
            MerchantRollingTransferConsumption(
                merchant_id=recovery.merchant_id,
                rolling_account_id=recovery.rolling_account_id,
                rolling_transfer_id=recovery.rolling_transfer_id,
                rolling_allocation_id=recovery.rolling_allocation_id,
                deposit_id=recovery.deposit_id,
                entry_type=RollingConsumptionType.reversal.value,
                amount_usdt=amount,
                amount_rub=money(recovery.amount_rub),
                rate_rub=recovery.rate_rub,
                idempotency_key=(
                    f'rolling-consumption:{recovery.rolling_transfer_id}:'
                    f'{deposit.id}:reversal'
                ),
                reason=normalized_reason[:500],
            )
        )

    account.recovered_usdt = usdt(account.recovered_usdt - applied_usdt)
    account.outstanding_usdt = usdt(
        account.outstanding_usdt + applied_usdt
    )
    if account.status != RollingAccountStatus.suspended.value:
        account.status = (
            RollingAccountStatus.active.value
            if account.outstanding_usdt > 0
            else RollingAccountStatus.exhausted.value
        )
    allocation.status = RollingAllocationStatus.reversed.value
    allocation.finalized_at = datetime.now(timezone.utc)
    db.add(
        MerchantRollingLedgerEntry(
            rolling_account_id=account.id,
            merchant_id=deposit.merchant_id,
            deposit_id=deposit.id,
            actor_id=actor_id,
            entry_type=RollingLedgerType.reversal.value,
            amount_usdt=applied_usdt,
            amount_rub=money(
                allocation.rolling_applied_rub + settle_credit
            ),
            rate_rub=allocation.rapira_rate_rub,
            reason=normalized_reason[:500],
            idempotency_key=f'rolling:{deposit.id}:reversal',
            metadata_json={
                'rolling_applied_rub': str(
                    money(allocation.rolling_applied_rub)
                ),
                'settle_reversed_rub': str(settle_credit),
            },
        )
    )
    await audit(
        db,
        'rolling_allocation_reversed',
        'deposit',
        actor_id,
        deposit.id,
        details={
            'rolling_applied_usdt': str(applied_usdt),
            'rolling_applied_rub': str(
                money(allocation.rolling_applied_rub)
            ),
            'settle_reversed_rub': str(settle_credit),
            'reason': normalized_reason,
        },
    )
    return allocation


async def rolling_overview(db: AsyncSession, merchant_id: UUID) -> dict:
    account = await get_rolling_account(db, merchant_id)
    status_counts = dict(
        (
            await db.execute(
                select(
                    MerchantRollingTransfer.status,
                    func.count(MerchantRollingTransfer.id),
                )
                .where(MerchantRollingTransfer.merchant_id == merchant_id)
                .group_by(MerchantRollingTransfer.status)
            )
        ).all()
    )
    confirmed_count = int(
        status_counts.get(RollingTransferStatus.confirmed.value, 0)
    )
    pending_count = int(
        status_counts.get(
            RollingTransferStatus.pending_confirmation.value,
            0,
        )
    )
    disputed_count = int(
        status_counts.get(RollingTransferStatus.disputed.value, 0)
    )
    pending_exposure = usdt(
        (
            await db.execute(
                select(
                    func.coalesce(
                        func.sum(
                            MerchantRollingAllocation.merchant_payable_usdt
                        ),
                        0,
                    )
                ).where(
                    MerchantRollingAllocation.merchant_id == merchant_id,
                    MerchantRollingAllocation.status
                    == RollingAllocationStatus.pending.value,
                    MerchantRollingAllocation.eligibility_status
                    == RollingEligibilityStatus.eligible.value,
                )
            )
        ).scalar_one()
    )
    principal = usdt(account.principal_usdt) if account else usdt(0)
    recovered = usdt(account.recovered_usdt) if account else usdt(0)
    outstanding = usdt(account.outstanding_usdt) if account else usdt(0)
    if account and account.status == RollingAccountStatus.suspended.value:
        status = RollingAccountStatus.suspended.value
    elif confirmed_count and outstanding > 0:
        status = RollingAccountStatus.active.value
    elif confirmed_count:
        status = RollingAccountStatus.exhausted.value
    elif disputed_count:
        status = RollingTransferStatus.disputed.value
    elif pending_count:
        status = RollingTransferStatus.pending_confirmation.value
    else:
        status = 'absent'
    return {
        'has_confirmed_rolling': confirmed_count > 0,
        'status': status,
        'principal': principal,
        'recovered': recovered,
        'outstanding': outstanding,
        'pending_transfers': pending_count,
        'disputed_transfers': disputed_count,
        'pending_exposure': pending_exposure,
    }


@dataclass(frozen=True)
class RollingReconciliationReport:
    merchant_id: UUID
    ok: bool
    expected_principal_usdt: Decimal
    actual_principal_usdt: Decimal
    expected_recovered_usdt: Decimal
    actual_recovered_usdt: Decimal
    expected_outstanding_usdt: Decimal
    actual_outstanding_usdt: Decimal
    pending_exposure_usdt: Decimal
    settle_overflow_rub: Decimal
    ledger_settle_overflow_rub: Decimal
    pending_transfer_count: int
    disputed_transfer_count: int
    transfer_consumption_mismatches: tuple[str, ...]
    paid_split_mismatches: tuple[str, ...]


async def reconcile_rolling_account(
    db: AsyncSession,
    merchant_id: UUID,
) -> RollingReconciliationReport | None:
    account = await get_rolling_account(db, merchant_id)
    transfers = list(
        (
            await db.execute(
                select(MerchantRollingTransfer).where(
                    MerchantRollingTransfer.merchant_id == merchant_id
                )
            )
        )
        .scalars()
        .all()
    )
    if not account and not transfers:
        return None
    confirmed = [
        row
        for row in transfers
        if row.status == RollingTransferStatus.confirmed.value
    ]
    expected_principal = usdt(
        sum((usdt(row.amount_usdt) for row in confirmed), Decimal('0'))
    )
    expected_recovered = usdt(
        sum((usdt(row.recovered_usdt) for row in confirmed), Decimal('0'))
    )
    expected_outstanding = usdt(
        sum((usdt(row.remaining_usdt) for row in confirmed), Decimal('0'))
    )
    pending_count = sum(
        row.status == RollingTransferStatus.pending_confirmation.value
        for row in transfers
    )
    disputed_count = sum(
        row.status == RollingTransferStatus.disputed.value
        for row in transfers
    )
    if not account:
        ok = not confirmed
        orphan_confirmed = tuple(str(row.id) for row in confirmed)
        if not ok:
            logger.error(
                'rolling_reconciliation_missing_account',
                extra={
                    'merchant_id': str(merchant_id),
                    'confirmed_transfer_count': len(confirmed),
                },
            )
        return RollingReconciliationReport(
            merchant_id=merchant_id,
            ok=ok,
            expected_principal_usdt=expected_principal,
            actual_principal_usdt=usdt(0),
            expected_recovered_usdt=expected_recovered,
            actual_recovered_usdt=usdt(0),
            expected_outstanding_usdt=expected_outstanding,
            actual_outstanding_usdt=usdt(0),
            pending_exposure_usdt=usdt(0),
            settle_overflow_rub=money(0),
            ledger_settle_overflow_rub=money(0),
            pending_transfer_count=pending_count,
            disputed_transfer_count=disputed_count,
            transfer_consumption_mismatches=orphan_confirmed,
            paid_split_mismatches=(),
        )
    consumptions = list(
        (
            await db.execute(
                select(MerchantRollingTransferConsumption).where(
                    MerchantRollingTransferConsumption.rolling_account_id
                    == account.id
                )
            )
        )
        .scalars()
        .all()
    )
    net_consumed_by_transfer: dict[UUID, Decimal] = {}
    for row in consumptions:
        sign = (
            Decimal('-1')
            if row.entry_type == RollingConsumptionType.reversal.value
            else Decimal('1')
        )
        net_consumed_by_transfer[row.rolling_transfer_id] = usdt(
            net_consumed_by_transfer.get(
                row.rolling_transfer_id,
                Decimal('0'),
            )
            + sign * usdt(row.amount_usdt)
        )
    consumption_mismatches = tuple(
        str(row.id)
        for row in confirmed
        if usdt(row.recovered_usdt)
        != usdt(net_consumed_by_transfer.get(row.id, Decimal('0')))
    )
    pending_exposure = usdt(
        (
            await db.execute(
                select(
                    func.coalesce(
                        func.sum(
                            MerchantRollingAllocation.merchant_payable_usdt
                        ),
                        0,
                    )
                ).where(
                    MerchantRollingAllocation.rolling_account_id == account.id,
                    MerchantRollingAllocation.status
                    == RollingAllocationStatus.pending.value,
                    MerchantRollingAllocation.eligibility_status
                    == RollingEligibilityStatus.eligible.value,
                )
            )
        ).scalar_one()
    )
    paid_allocations = list(
        (
            await db.execute(
                select(MerchantRollingAllocation).where(
                    MerchantRollingAllocation.rolling_account_id == account.id,
                    MerchantRollingAllocation.status
                    == RollingAllocationStatus.paid.value,
                )
            )
        )
        .scalars()
        .all()
    )
    settle_overflow = money(
        sum(
            (money(row.settle_credited_rub) for row in paid_allocations),
            Decimal('0.00'),
        )
    )
    ledger = list(
        (
            await db.execute(
                select(MerchantRollingLedgerEntry).where(
                    MerchantRollingLedgerEntry.rolling_account_id == account.id
                )
            )
        )
        .scalars()
        .all()
    )
    ledger_settle_overflow = money(
        sum(
            (
                money(row.amount_rub)
                for row in ledger
                if row.entry_type == RollingLedgerType.settle_overflow.value
            ),
            Decimal('0.00'),
        )
        - sum(
            (
                money((row.metadata_json or {}).get('settle_reversed_rub', 0))
                for row in ledger
                if row.entry_type == RollingLedgerType.reversal.value
            ),
            Decimal('0.00'),
        )
    )
    split_mismatches = tuple(
        str(row.deposit_id)
        for row in paid_allocations
        if money(row.rolling_applied_rub + row.settle_credited_rub)
        != money(row.merchant_payable_rub)
    )
    actual_principal = usdt(account.principal_usdt)
    actual_recovered = usdt(account.recovered_usdt)
    actual_outstanding = usdt(account.outstanding_usdt)
    ok = (
        expected_principal == actual_principal
        and expected_recovered == actual_recovered
        and expected_outstanding == actual_outstanding
        and expected_principal
        == usdt(expected_recovered + expected_outstanding)
        and ledger_settle_overflow == settle_overflow
        and not consumption_mismatches
        and not split_mismatches
    )
    if not ok:
        logger.error(
            'rolling_reconciliation_mismatch',
            extra={
                'merchant_id': str(merchant_id),
                'principal_delta': str(
                    actual_principal - expected_principal
                ),
                'recovered_delta': str(
                    actual_recovered - expected_recovered
                ),
                'outstanding_delta': str(
                    actual_outstanding - expected_outstanding
                ),
                'consumption_mismatch_count': len(
                    consumption_mismatches
                ),
                'split_mismatch_count': len(split_mismatches),
            },
        )
    return RollingReconciliationReport(
        merchant_id=merchant_id,
        ok=ok,
        expected_principal_usdt=expected_principal,
        actual_principal_usdt=actual_principal,
        expected_recovered_usdt=expected_recovered,
        actual_recovered_usdt=actual_recovered,
        expected_outstanding_usdt=expected_outstanding,
        actual_outstanding_usdt=actual_outstanding,
        pending_exposure_usdt=pending_exposure,
        settle_overflow_rub=settle_overflow,
        ledger_settle_overflow_rub=ledger_settle_overflow,
        pending_transfer_count=pending_count,
        disputed_transfer_count=disputed_count,
        transfer_consumption_mismatches=consumption_mismatches,
        paid_split_mismatches=split_mismatches,
    )
