"""Presentation-only formatting for TeamLead percentages and USDT amounts."""

from decimal import Decimal


def teamlead_percentage(value: Decimal | None) -> str:
    """Stored values already represent percentages; keep their scale unchanged."""
    return "—" if value is None else format(value, ".2f")


def teamlead_usdt(value: Decimal | None) -> str:
    """Format a read-only USDT amount; never use this value for accounting."""
    return "—" if value is None else format(value, ".2f")
