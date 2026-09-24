from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.core.config import settings


RAPIRA_SYMBOLS = {'USDT/RUB', 'USDTRUB', 'USDT_RUB'}
RUB_USDT_SYMBOLS = {'RUB/USDT', 'RUBUSDT', 'RUB_USDT'}
_cache_value: Decimal | None = None
_cache_expires_at: datetime | None = None
_last_success_value: Decimal | None = None
_last_success_at: datetime | None = None
_strict_rolling_cache: RollingRapiraQuote | None = None
_PROVIDER_FUTURE_SKEW = timedelta(seconds=5)


@dataclass(frozen=True)
class RapiraRateQuote:
    rate_rub: Decimal
    source: str
    updated_at: datetime
    stale: bool
    cache_seconds: int


@dataclass(frozen=True)
class RollingRapiraQuote:
    symbol: str
    rate: Decimal
    side: str
    source: str
    provider_timestamp: datetime | None
    fetched_at: datetime
    freshness_basis: str
    stale: bool
    provider_field: str

    @property
    def updated_at(self) -> datetime:
        return self.provider_timestamp or self.fetched_at


class RollingRateUnavailable(ValueError):
    code = 'rolling_rate_unavailable'


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


def _pick_price(row: dict[str, Any]) -> Decimal | None:
    for key in ('askPrice', 'last', 'close', 'bidPrice', 'price', 'rate'):
        price = _to_decimal(row.get(key))
        if price:
            return price
    return None


def _normalize_symbol(value: Any) -> str:
    return str(value or '').upper().replace('-', '/').strip()


def parse_rapira_market_payload(payload: Any) -> list[dict[str, Any]]:
    """Return provider market rows without applying a quote policy."""
    rows = payload.get('data') if isinstance(payload, dict) else payload
    if isinstance(rows, dict):
        rows = rows.get('items') or rows.get('rates') or rows.get('data') or list(rows.values())
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _symbol_direction(symbol: str) -> str | None:
    compact = symbol.replace('/', '')
    if symbol in RAPIRA_SYMBOLS or compact in RAPIRA_SYMBOLS:
        return 'direct'
    if symbol in RUB_USDT_SYMBOLS or compact in RUB_USDT_SYMBOLS:
        return 'inverse'
    return None


def _metadata_direction(row: dict[str, Any]) -> str | None:
    base = _normalize_symbol(row.get('baseCurrency'))
    quote = _normalize_symbol(row.get('quoteCurrency'))
    if base == 'USDT' and quote == 'RUB':
        return 'direct'
    if base == 'RUB' and quote == 'USDT':
        return 'inverse'
    return None


def _pair_direction(row: dict[str, Any]) -> str | None:
    """Preserve the legacy symbol-first metadata fallback policy."""
    symbol = _normalize_symbol(row.get('symbol') or row.get('pair') or row.get('market'))
    return _symbol_direction(symbol) or _metadata_direction(row)


def _strict_pair_direction(row: dict[str, Any]) -> str | None:
    """Use only a recognized canonical symbol for a strict Rolling quote."""
    symbol = _normalize_symbol(row.get('symbol') or row.get('pair') or row.get('market'))
    direction = _symbol_direction(symbol)
    if direction is None:
        return None

    metadata_direction = _metadata_direction(row)
    if metadata_direction and metadata_direction != direction:
        logging.getLogger('app.finance').warning(
            'rolling_rapira_pair_metadata_conflict',
            extra={
                'symbol': symbol,
                'base_currency': _normalize_symbol(row.get('baseCurrency')),
                'quote_currency': _normalize_symbol(row.get('quoteCurrency')),
            },
        )
    return direction


def _provider_datetime(value: Any) -> datetime | None:
    if value in (None, ''):
        return None
    if isinstance(value, (int, float, Decimal)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def parse_strict_rolling_ask_quote(
    payload: Any,
    *,
    fetched_at: datetime | None = None,
    evaluated_at: datetime | None = None,
) -> RollingRapiraQuote | None:
    fetched_at = fetched_at or datetime.now(timezone.utc)
    evaluated_at = evaluated_at or fetched_at
    if fetched_at.tzinfo is None or evaluated_at.tzinfo is None:
        return None
    fetched_at = fetched_at.astimezone(timezone.utc)
    evaluated_at = evaluated_at.astimezone(timezone.utc)
    max_age = timedelta(seconds=settings.ROLLING_RAPIRA_MAX_AGE_SECONDS)
    fetched_age = evaluated_at - fetched_at
    if fetched_age > max_age or fetched_age < -_PROVIDER_FUTURE_SKEW:
        return None

    for row in parse_rapira_market_payload(payload):
        direction = _strict_pair_direction(row)
        if direction is None:
            continue
        ask = _to_decimal(row.get('askPrice'))
        if not ask:
            continue
        rate = ask if direction == 'direct' else Decimal('1') / ask
        raw_provider_timestamp = next(
            (
                row.get(field)
                for field in ('updatedAt', 'updated_at', 'timestamp', 'time')
                if row.get(field) not in (None, '')
            ),
            None,
        )
        provider_timestamp = _provider_datetime(raw_provider_timestamp)
        if raw_provider_timestamp is not None:
            if provider_timestamp is None:
                continue
            provider_age = fetched_at - provider_timestamp
            if provider_age > max_age or provider_age < -_PROVIDER_FUTURE_SKEW:
                continue
        freshness_basis = (
            'provider_timestamp'
            if provider_timestamp is not None
            else 'fetched_at'
        )
        return RollingRapiraQuote(
            symbol='USDT/RUB',
            rate=rate,
            side='ask',
            source='rapira_live',
            provider_timestamp=provider_timestamp,
            fetched_at=fetched_at,
            freshness_basis=freshness_basis,
            stale=False,
            provider_field='askPrice',
        )
    return None


def parse_rapira_rub_usdt_rate(payload: Any) -> Decimal | None:
    for row in parse_rapira_market_payload(payload):
        price = _pick_price(row)
        if not price:
            continue
        direction = _pair_direction(row)
        if direction == 'direct':
            return price
        if direction == 'inverse':
            return Decimal('1') / price
    return None


async def fetch_rapira_rub_usdt_rate() -> Decimal | None:
    global _cache_value, _cache_expires_at, _last_success_value, _last_success_at
    now = datetime.now(timezone.utc)
    if _cache_value and _cache_expires_at and _cache_expires_at > now:
        return _cache_value

    url = settings.RAPIRA_RATES_URL.strip()
    if not settings.RAPIRA_RATES_ENABLED or not url:
        return None

    timeout = httpx.Timeout(settings.RAPIRA_RATES_TIMEOUT_SECONDS)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.get(url, headers={'accept': 'application/json'})
            response.raise_for_status()
            parsed = parse_rapira_rub_usdt_rate(response.json())
    except (httpx.HTTPError, ValueError):
        return None

    if parsed and parsed > 0:
        _cache_value = parsed
        _last_success_value = parsed
        _last_success_at = now
        _cache_expires_at = now + timedelta(seconds=max(5, settings.RAPIRA_RATES_CACHE_SECONDS))
        return parsed
    return None


async def get_strict_rolling_ask_quote() -> RollingRapiraQuote:
    """Return a fresh direct USDT/RUB Rapira ask without any fallback."""
    global _strict_rolling_cache
    now = datetime.now(timezone.utc)
    max_age = timedelta(seconds=settings.ROLLING_RAPIRA_MAX_AGE_SECONDS)
    url = settings.RAPIRA_RATES_URL.strip()
    if not settings.RAPIRA_RATES_ENABLED or not url:
        raise RollingRateUnavailable('rolling_rate_unavailable')

    if _strict_rolling_cache:
        freshness_age = now - _strict_rolling_cache.updated_at
        cache_age = now - _strict_rolling_cache.fetched_at
        strict_cache_seconds = min(
            60,
            settings.ROLLING_RAPIRA_MAX_AGE_SECONDS,
            max(1, settings.RAPIRA_RATES_CACHE_SECONDS),
        )
        if (
            _strict_rolling_cache.symbol == 'USDT/RUB'
            and _strict_rolling_cache.side == 'ask'
            and _strict_rolling_cache.source == 'rapira_live'
            and _strict_rolling_cache.provider_field == 'askPrice'
            and -_PROVIDER_FUTURE_SKEW <= freshness_age <= max_age
            and timedelta(0) <= cache_age <= timedelta(seconds=strict_cache_seconds)
        ):
            return _strict_rolling_cache

    timeout = httpx.Timeout(settings.RAPIRA_RATES_TIMEOUT_SECONDS)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.get(url, headers={'accept': 'application/json'})
            response.raise_for_status()
            fetched_at = datetime.now(timezone.utc)
            quote = parse_strict_rolling_ask_quote(
                response.json(),
                fetched_at=fetched_at,
                evaluated_at=fetched_at,
            )
    except (httpx.HTTPError, ValueError) as exc:
        logging.getLogger('app.finance').warning(
            'rolling_rapira_quote_unavailable',
            extra={'error_type': type(exc).__name__},
        )
        raise RollingRateUnavailable('rolling_rate_unavailable') from exc
    if not quote:
        raise RollingRateUnavailable('rolling_rate_unavailable')
    _strict_rolling_cache = quote
    return quote


def _configured_fallback_rate() -> Decimal:
    rate = _to_decimal(settings.SETTLEMENT_USDT_RUB_RATE)
    return rate if rate else Decimal('100.0000')


async def get_rapira_rub_usdt_quote() -> RapiraRateQuote:
    now = datetime.now(timezone.utc)
    if _cache_value and _cache_expires_at and _cache_expires_at > now:
        return RapiraRateQuote(
            rate_rub=_cache_value,
            source='Rapira',
            updated_at=_last_success_at or now,
            stale=False,
            cache_seconds=settings.RAPIRA_RATES_CACHE_SECONDS,
        )

    live_rate = await fetch_rapira_rub_usdt_rate()
    if live_rate:
        return RapiraRateQuote(
            rate_rub=live_rate,
            source='Rapira',
            updated_at=_last_success_at or now,
            stale=False,
            cache_seconds=settings.RAPIRA_RATES_CACHE_SECONDS,
        )

    if _last_success_value:
        return RapiraRateQuote(
            rate_rub=_last_success_value,
            source='Rapira',
            updated_at=_last_success_at or now,
            stale=True,
            cache_seconds=settings.RAPIRA_RATES_CACHE_SECONDS,
        )

    return RapiraRateQuote(
        rate_rub=_configured_fallback_rate(),
        source='SETTLEMENT_USDT_RUB_RATE',
        updated_at=now,
        stale=True,
        cache_seconds=settings.RAPIRA_RATES_CACHE_SECONDS,
    )
