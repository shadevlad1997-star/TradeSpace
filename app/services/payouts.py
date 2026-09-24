from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import PayoutStatus
from app.models import Payout
from app.services.ledger import LedgerError, debit_frozen, release_hold


PAYOUT_COMPLETABLE_STATUSES = {PayoutStatus.pending.value, PayoutStatus.processing.value}
PAYOUT_REVERSIBLE_STATUSES = {PayoutStatus.pending.value, PayoutStatus.processing.value}


def _amount(payout: Payout) -> Decimal:
    return Decimal(payout.amount or '0.00')


async def complete_payout(db: AsyncSession, payout: Payout) -> None:
    if payout.status not in PAYOUT_COMPLETABLE_STATUSES:
        raise ValueError(f'payout status {payout.status} cannot be completed')
    try:
        await debit_frozen(
            db,
            payout.merchant_id,
            _amount(payout),
            payout.id,
            f'payout-complete:{payout.id}',
            'payout completed',
        )
    except LedgerError as exc:
        raise ValueError(str(exc)) from exc
    payout.status = PayoutStatus.completed.value


async def release_payout_hold(db: AsyncSession, payout: Payout, final_status: PayoutStatus | str, description: str) -> None:
    final_status_value = final_status.value if isinstance(final_status, PayoutStatus) else str(final_status)
    if payout.status not in PAYOUT_REVERSIBLE_STATUSES:
        raise ValueError(f'payout status {payout.status} cannot be moved to {final_status_value}')
    if final_status_value not in {
        PayoutStatus.cancelled.value,
        PayoutStatus.rejected.value,
        PayoutStatus.failed.value,
    }:
        raise ValueError('invalid payout final status')
    try:
        await release_hold(
            db,
            payout.merchant_id,
            _amount(payout),
            payout.id,
            f'payout-{final_status_value}:{payout.id}',
            description,
        )
    except LedgerError as exc:
        raise ValueError(str(exc)) from exc
    payout.status = final_status_value
