"""Read-only TeamLead settlement estimate for the presentation layer."""

from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import TeamLeadBalance
from app.services.rapira import get_strict_rolling_ask_quote
from app.services.teamlead import (
    TEAMLEAD_SETTLEMENT_FEE_USDT, TeamLeadSettlementUnavailable, _validate_live_quote,
)

RUB_CENT = Decimal("0.01")
USDT_CENT = Decimal("0.01")


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def affordable_usdt(available_rub: Decimal | None, rate: Decimal) -> Decimal:
    """Largest cent-denominated request whose separately rounded RUB costs fit.

    This never reserves funds or determines the amount accepted by the command.
    """
    available = available_rub if available_rub is not None else Decimal("0")
    fee_rub = (TEAMLEAD_SETTLEMENT_FEE_USDT * rate).quantize(RUB_CENT, rounding=ROUND_HALF_UP)
    if available <= fee_rub:
        return Decimal("0.00")

    # One extra RUB cent safely brackets any amount whose separately rounded
    # request cost fits; binary search tests the command's exact RUB rule.
    upper = int((((available - fee_rub + RUB_CENT) / rate) / USDT_CENT)
                .to_integral_value(rounding=ROUND_FLOOR)) + 1
    low, high = 0, upper
    while low < high:
        mid = (low + high + 1) // 2
        requested = Decimal(mid) * USDT_CENT
        request_rub = (requested * rate).quantize(RUB_CENT, rounding=ROUND_HALF_UP)
        if request_rub + fee_rub <= available:
            low = mid
        else:
            high = mid - 1
    return Decimal(low) * USDT_CENT


async def read_teamlead_estimate(db: AsyncSession, teamlead_id) -> dict:
    # The strict cached provider fetch precedes the balance SELECT. No balance
    # helper that creates a missing row is used on this read path.
    quote = await get_strict_rolling_ask_quote()
    available = await db.scalar(select(TeamLeadBalance.available_rub).where(
        TeamLeadBalance.teamlead_id == teamlead_id
    ))
    now = datetime.now(timezone.utc)
    rate = _validate_live_quote(quote, now=now)
    remaining = min(
        settings.ROLLING_RAPIRA_MAX_AGE_SECONDS - (now - _utc(quote.fetched_at)).total_seconds(),
        settings.ROLLING_RAPIRA_MAX_AGE_SECONDS - (now - _utc(quote.updated_at)).total_seconds(),
    )
    if remaining < 1:
        raise TeamLeadSettlementUnavailable('teamlead_rate_unavailable')
    return {
        "available_usdt": format(affordable_usdt(available, rate), ".2f"),
        "valid_for_seconds": int(remaining),
        "source": "rapira_live",
    }
