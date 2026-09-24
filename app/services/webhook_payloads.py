from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any


SENSITIVE_PAYLOAD_KEY_PARTS = (
    'secret',
    'token',
    'password',
    'authorization',
    'api_key',
    'apikey',
    'hmac',
)


def _money(value: Any) -> str:
    return str(Decimal(value or '0.00').quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def _aware(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo:
        return value.astimezone(timezone.utc)
    return value.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str:
    return _aware(value).isoformat().replace('+00:00', 'Z')


def _safe_extra(value: Any) -> Any:
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            key_text = str(key)
            normalized = key_text.lower().replace('-', '_')
            if any(part in normalized for part in SENSITIVE_PAYLOAD_KEY_PARTS):
                continue
            clean[key_text] = _safe_extra(item)
        return clean
    if isinstance(value, list):
        return [_safe_extra(item) for item in value]
    if isinstance(value, Decimal):
        return _money(value)
    if isinstance(value, datetime):
        return _iso(value)
    return value


def _merge_extra(payload: dict, extra: dict | None) -> dict:
    if extra:
        payload.update(_safe_extra(extra))
    return payload


def build_deposit_webhook_payload(deposit, event_type: str | None = None, extra: dict | None = None) -> dict:
    payload = {
        'id': str(deposit.id),
        'external_id': deposit.external_id,
        'operation_type': 'deposit',
        'status': deposit.status,
        'amount': _money(deposit.amount),
        'currency': deposit.currency,
        'method': deposit.method,
        'payment_method': deposit.method,
        'created_at': _iso(getattr(deposit, 'created_at', None)),
        'updated_at': _iso(getattr(deposit, 'updated_at', None)),
    }
    return _merge_extra(payload, extra)


def build_payout_webhook_payload(payout, event_type: str | None = None, extra: dict | None = None) -> dict:
    payload = {
        'id': str(payout.id),
        'external_id': payout.external_id,
        'operation_type': 'payout',
        'status': payout.status,
        'amount': _money(payout.amount),
        'currency': payout.currency,
        'method': payout.method,
        'payment_method': payout.method,
        'destination': payout.destination,
        'created_at': _iso(getattr(payout, 'created_at', None)),
        'updated_at': _iso(getattr(payout, 'updated_at', None)),
    }
    return _merge_extra(payload, extra)
