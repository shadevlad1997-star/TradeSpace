from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.models import Deposit


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: datetime) -> datetime:
    return (
        value.astimezone(timezone.utc)
        if value.tzinfo
        else value.replace(tzinfo=timezone.utc)
    )


def new_deposit_expires_at(now: datetime | None = None) -> datetime:
    current = aware(now) if now else utcnow()
    return current + timedelta(seconds=settings.DEPOSIT_PROCESSING_TTL_SECONDS)


def deposit_deadline(deposit: Deposit) -> datetime:
    expires_at = getattr(deposit, 'expires_at', None)
    if expires_at is not None:
        return aware(expires_at)
    # Compatibility for in-memory legacy objects while the migration is staged.
    return aware(deposit.created_at) + timedelta(
        seconds=settings.DEPOSIT_PROCESSING_TTL_SECONDS
    )


def deposit_is_expired(
    deposit: Deposit,
    *,
    now: datetime | None = None,
) -> bool:
    return deposit_deadline(deposit) <= (aware(now) if now else utcnow())


def deposit_remaining_seconds(
    deposit: Deposit,
    *,
    now: datetime | None = None,
) -> int:
    current = aware(now) if now else utcnow()
    return max(0, int((deposit_deadline(deposit) - current).total_seconds()))
