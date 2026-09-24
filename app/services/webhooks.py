import asyncio
import ipaddress
import json
import logging
import secrets
import socket
import ssl
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.branding import get_branding
from app.core.config import settings
from app.core.metrics import metrics_registry
from app.core.security import sign_hmac
from app.core.validators import validate_public_webhook_url
from app.models import (
    Merchant,
    WebhookDeliveryAttempt,
    WebhookEvent,
)
from app.services.webhook_signing_keys import (
    delivery_webhook_signing_key,
)


logger = logging.getLogger('app.webhooks')
TERMINAL_WEBHOOK_STATUSES = {
    'delivered',
    'dead_letter',
    'configuration_required',
}
RETRYABLE_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}
LEGACY_STATUS_MAP = {
    'queued': 'pending',
    'delivering': 'processing',
    'failed': 'retry_scheduled',
    'retry': 'retry_scheduled',
    'configuration_error': 'configuration_required',
    'blocked': 'dead_letter',
    'skipped': 'configuration_required',
}


@dataclass(frozen=True)
class WebhookHttpResult:
    status_code: int
    retry_after: str | None = None


@dataclass(frozen=True)
class WebhookClaim:
    event_id: uuid.UUID
    event_type: str
    merchant_id: uuid.UUID
    attempt_id: uuid.UUID
    attempt_no: int
    lock_owner: str
    webhook_url: str
    body: bytes
    headers: dict[str, str]


def _webhook_user_agent() -> str:
    brand_id = ''.join(
        ch
        for ch in get_branding().brand_id
        if ch.isalnum() or ch in {'-', '_'}
    ).strip('-_')
    return f'{brand_id or "platform"}-Webhook/2.0'


def _is_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    return any(
        (
            ip.is_private,
            ip.is_loopback,
            ip.is_link_local,
            ip.is_reserved,
            ip.is_multicast,
            ip.is_unspecified,
        )
    )


async def _resolve_webhook_ip(host: str, port: int) -> str:
    infos = await asyncio.to_thread(
        socket.getaddrinfo,
        host,
        port,
        type=socket.SOCK_STREAM,
    )
    candidates: list[str] = []
    for info in infos:
        try:
            parsed = ipaddress.ip_address(info[4][0])
        except (ValueError, IndexError):
            continue
        if settings.is_production and _is_blocked_ip(parsed):
            raise ValueError(
                'webhook_url must not resolve to private or reserved IP ranges'
            )
        candidates.append(str(parsed))
    if not candidates:
        raise ValueError('webhook_url host could not be resolved')
    return candidates[0]


async def _post_validated_webhook(
    url: str,
    body: bytes,
    headers: dict[str, str],
    timeout_seconds: float = 10.0,
) -> WebhookHttpResult:
    parsed = urlparse(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        raise ValueError('webhook_url is invalid')
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    host = parsed.hostname.strip().lower().rstrip('.')
    connect_host = host
    if settings.is_production:
        connect_host = await _resolve_webhook_ip(host, port)
    ssl_context = (
        ssl.create_default_context()
        if parsed.scheme == 'https'
        else None
    )
    server_hostname = host if parsed.scheme == 'https' else None
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(
            connect_host,
            port,
            ssl=ssl_context,
            server_hostname=server_hostname,
        ),
        timeout=timeout_seconds,
    )
    try:
        path = parsed.path or '/'
        if parsed.query:
            path += '?' + parsed.query
        default_port = (
            (parsed.scheme == 'https' and port == 443)
            or (parsed.scheme == 'http' and port == 80)
        )
        host_header = host if default_port else f'{host}:{port}'
        request_headers = {
            'Host': host_header,
            'User-Agent': _webhook_user_agent(),
            'Content-Length': str(len(body)),
            'Connection': 'close',
            **headers,
        }
        header_blob = ''.join(
            f'{name}: {value}\r\n'
            for name, value in request_headers.items()
        )
        writer.write(
            f'POST {path} HTTP/1.1\r\n{header_blob}\r\n'.encode('ascii')
            + body
        )
        await asyncio.wait_for(writer.drain(), timeout=timeout_seconds)
        status_line = await asyncio.wait_for(
            reader.readline(),
            timeout=timeout_seconds,
        )
        if not status_line:
            raise ValueError('webhook response is empty')
        parts = status_line.decode(
            'iso-8859-1',
            errors='replace',
        ).split()
        if len(parts) < 2 or not parts[1].isdigit():
            raise ValueError('webhook response status is invalid')

        retry_after = None
        for _ in range(100):
            line = await asyncio.wait_for(
                reader.readline(),
                timeout=timeout_seconds,
            )
            if len(line) > 8192:
                raise ValueError('webhook response header is too large')
            if line in {b'', b'\r\n', b'\n'}:
                break
            name, separator, value = line.partition(b':')
            if (
                separator
                and name.strip().lower() == b'retry-after'
            ):
                retry_after = value.decode(
                    'iso-8859-1',
                    errors='replace',
                ).strip()[:128]
        return WebhookHttpResult(
            status_code=int(parts[1]),
            retry_after=retry_after,
        )
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


def _parse_retry_after(
    value: str | None,
    *,
    now: datetime,
) -> int | None:
    candidate = (value or '').strip()
    if not candidate:
        return None
    if candidate.isdigit():
        seconds = int(candidate)
    else:
        try:
            target = parsedate_to_datetime(candidate)
        except (TypeError, ValueError, OverflowError):
            return None
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        seconds = max(
            0,
            int(
                (
                    target.astimezone(timezone.utc)
                    - now.astimezone(timezone.utc)
                ).total_seconds()
            ),
        )
    return min(seconds, settings.WEBHOOK_RETRY_AFTER_MAX_SECONDS)


def webhook_retry_delay_seconds(
    attempt_no: int,
    *,
    retry_after: str | None = None,
    now: datetime | None = None,
) -> int:
    current = now or datetime.now(timezone.utc)
    exponent = max(0, min(attempt_no - 1, 16))
    backoff = min(
        settings.WEBHOOK_RETRY_MAX_SECONDS,
        settings.WEBHOOK_RETRY_BASE_SECONDS * (2 ** exponent),
    )
    jitter = (
        secrets.randbelow(settings.WEBHOOK_RETRY_JITTER_SECONDS + 1)
        if settings.WEBHOOK_RETRY_JITTER_SECONDS
        else 0
    )
    server_delay = _parse_retry_after(retry_after, now=current) or 0
    return min(
        settings.WEBHOOK_RETRY_MAX_SECONDS,
        max(backoff + jitter, server_delay),
    )


def _normalize_legacy_status(event: WebhookEvent) -> None:
    event.status = LEGACY_STATUS_MAP.get(event.status, event.status)
    if event.next_attempt_at is None and event.next_retry_at is not None:
        event.next_attempt_at = event.next_retry_at


def _release_lease(event: WebhookEvent) -> None:
    event.lock_owner = None
    event.locked_at = None
    event.lease_until = None


def _configuration_required(
    event: WebhookEvent,
    *,
    reason: str,
) -> None:
    event.status = 'configuration_required'
    event.last_error = reason
    event.last_status_code = None
    event.response_snippet = ''
    event.next_attempt_at = None
    event.next_retry_at = None
    _release_lease(event)
    metrics_registry.increment(
        'processing_platform_webhook_events_total',
        {
            'event_type': event.event_type,
            'status': 'configuration_required',
        },
    )
    logger.warning(
        'webhook_configuration_required',
        extra={
            'event_id': str(event.id),
            'merchant_id': str(event.merchant_id),
            'event_type': event.event_type,
            'reason': reason,
        },
    )


async def queue_webhook(
    db: AsyncSession,
    merchant_id,
    event_type: str,
    payload: dict,
):
    event = WebhookEvent(
        merchant_id=merchant_id,
        event_type=event_type,
        payload=payload,
        status='pending',
        attempts=0,
        max_attempts=settings.PLATFORM_WEBHOOK_RETRY_LIMIT,
        correlation_id=uuid.uuid4().hex,
    )
    db.add(event)
    await db.flush()
    metrics_registry.increment(
        'processing_platform_webhook_events_total',
        {'event_type': event_type, 'status': 'pending'},
    )
    logger.info(
        'webhook_queued',
        extra={
            'event_id': str(event.id),
            'merchant_id': str(merchant_id),
            'event_type': event_type,
        },
    )
    return event


async def _claim_event(
    db: AsyncSession,
    event_id,
    *,
    lock_owner: str,
    now: datetime,
) -> WebhookClaim | None:
    try:
        event_uuid = uuid.UUID(str(event_id))
    except (TypeError, ValueError):
        return None
    event = (
        await db.execute(
            select(WebhookEvent)
            .where(WebhookEvent.id == event_uuid)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not event:
        return None
    _normalize_legacy_status(event)
    if event.status in TERMINAL_WEBHOOK_STATUSES:
        return None
    if (
        event.status == 'processing'
        and event.lease_until
        and event.lease_until > now
    ):
        return None
    if (
        event.status == 'retry_scheduled'
        and event.next_attempt_at
        and event.next_attempt_at > now
    ):
        return None
    if event.status not in {'pending', 'processing', 'retry_scheduled'}:
        event.status = 'dead_letter'
        event.last_error = 'invalid delivery state'
        event.next_attempt_at = None
        event.next_retry_at = None
        _release_lease(event)
        return None
    if event.attempts >= event.max_attempts:
        event.status = 'dead_letter'
        event.last_error = 'retry limit reached'
        event.next_attempt_at = None
        event.next_retry_at = None
        _release_lease(event)
        return None

    merchant = (
        await db.execute(
            select(Merchant).where(Merchant.id == event.merchant_id)
        )
    ).scalar_one()
    if not merchant.webhook_url:
        _configuration_required(event, reason='missing_webhook_url')
        return None
    signing = await delivery_webhook_signing_key(
        db,
        merchant.id,
        event.signing_key_id,
    )
    if not signing:
        _configuration_required(
            event,
            reason='missing_webhook_signing_key',
        )
        return None
    signing_key, signing_secret = signing

    body = json.dumps(
        {'event': event.event_type, 'payload': event.payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    timestamp = str(int(time.time()))
    signature = sign_hmac(signing_secret, timestamp, body)
    event.status = 'processing'
    event.attempts += 1
    event.signing_key_id = signing_key.id
    event.lock_owner = lock_owner
    event.locked_at = now
    event.lease_until = now + timedelta(
        seconds=settings.WEBHOOK_LEASE_SECONDS
    )
    event.next_attempt_at = None
    event.next_retry_at = None
    attempt = WebhookDeliveryAttempt(
        webhook_event_id=event.id,
        attempt_no=event.attempts,
        status='processing',
    )
    db.add(attempt)
    await db.flush()
    return WebhookClaim(
        event_id=event.id,
        event_type=event.event_type,
        merchant_id=event.merchant_id,
        attempt_id=attempt.id,
        attempt_no=event.attempts,
        lock_owner=lock_owner,
        webhook_url=merchant.webhook_url,
        body=body,
        headers={
            'Content-Type': 'application/json',
            'X-TradeSpace-Event-ID': str(event.id),
            'X-TradeSpace-Event-Type': event.event_type,
            'X-TradeSpace-Timestamp': timestamp,
            'X-TradeSpace-Key-ID': signing_key.key_id,
            'X-TradeSpace-Signature-Version': '1',
            'X-TradeSpace-Signature': signature,
            'X-Correlation-ID': event.correlation_id,
        },
    )


def _redacted_exception(exc: Exception) -> str:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return 'network_timeout'
    if isinstance(exc, OSError):
        return 'network_error'
    if isinstance(exc, ValueError) and 'webhook_url' in str(exc):
        return str(exc)[:300]
    return f'delivery_error:{type(exc).__name__}'


async def _finalize_event(
    db: AsyncSession,
    claim: WebhookClaim,
    *,
    status_code: int | None,
    error: str | None,
    retryable: bool,
    retry_after: str | None,
    now: datetime,
) -> WebhookEvent:
    event = (
        await db.execute(
            select(WebhookEvent)
            .where(WebhookEvent.id == claim.event_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    attempt = (
        await db.execute(
            select(WebhookDeliveryAttempt)
            .where(WebhookDeliveryAttempt.id == claim.attempt_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if (
        event.status != 'processing'
        or event.lock_owner != claim.lock_owner
        or event.attempts != claim.attempt_no
    ):
        return event

    delivered = status_code is not None and 200 <= status_code < 300
    if delivered:
        final_status = 'delivered'
    elif retryable and event.attempts < event.max_attempts:
        final_status = 'retry_scheduled'
    else:
        final_status = 'dead_letter'

    event.status = final_status
    event.last_status_code = status_code
    event.response_snippet = ''
    event.last_error = None if delivered else (error or 'delivery_failed')
    if final_status == 'retry_scheduled':
        event.next_attempt_at = now + timedelta(
            seconds=webhook_retry_delay_seconds(
                event.attempts,
                retry_after=retry_after,
                now=now,
            )
        )
    else:
        event.next_attempt_at = None
    event.next_retry_at = event.next_attempt_at
    _release_lease(event)

    attempt.status = final_status
    attempt.status_code = status_code
    attempt.response_snippet = ''
    attempt.error = event.last_error
    metrics_registry.increment(
        'processing_platform_webhook_events_total',
        {'event_type': event.event_type, 'status': final_status},
    )
    log_method = logger.info if delivered else logger.warning
    log_method(
        (
            'webhook_delivered'
            if delivered
            else 'webhook_delivery_not_delivered'
        ),
        extra={
            'event_id': str(event.id),
            'merchant_id': str(event.merchant_id),
            'event_type': event.event_type,
            'status_code': status_code,
            'status': final_status,
            'reason': event.last_error,
        },
    )
    return event


async def deliver_event(
    db: AsyncSession,
    event_id,
    *,
    lock_owner: str | None = None,
    http_post=None,
):
    owner = (lock_owner or uuid.uuid4().hex)[:64]
    claim = await _claim_event(
        db,
        event_id,
        lock_owner=owner,
        now=datetime.now(timezone.utc),
    )
    if not claim:
        await db.flush()
        try:
            event_uuid = uuid.UUID(str(event_id))
        except (TypeError, ValueError):
            return None
        return (
            await db.execute(
                select(WebhookEvent).where(WebhookEvent.id == event_uuid)
            )
        ).scalar_one_or_none()

    # The claim is durable before DNS or HTTP. No row lock is held while the
    # external endpoint is contacted.
    await db.commit()
    poster = http_post or _post_validated_webhook
    status_code = None
    retry_after = None
    error = None
    retryable = True
    try:
        await asyncio.to_thread(
            validate_public_webhook_url,
            claim.webhook_url,
        )
        result = await poster(
            claim.webhook_url,
            claim.body,
            claim.headers,
            timeout_seconds=10.0,
        )
        if isinstance(result, int):
            status_code = result
        else:
            status_code = result.status_code
            retry_after = result.retry_after
        retryable = status_code in RETRYABLE_HTTP_STATUSES
        if not 200 <= status_code < 300:
            error = f'HTTP {status_code}'
    except Exception as exc:
        error = _redacted_exception(exc)
        retryable = not (
            isinstance(exc, ValueError)
            and 'webhook_url' in str(exc)
        )

    return await _finalize_event(
        db,
        claim,
        status_code=status_code,
        error=error,
        retryable=retryable,
        retry_after=retry_after,
        now=datetime.now(timezone.utc),
    )


async def due_webhook_event_ids(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> list[uuid.UUID]:
    current = now or datetime.now(timezone.utc)
    return (
        await db.execute(
            select(WebhookEvent.id)
            .where(
                or_(
                    WebhookEvent.status.in_(['pending', 'queued']),
                    and_(
                        WebhookEvent.status.in_(
                            ['retry_scheduled', 'failed', 'retry']
                        ),
                        or_(
                            WebhookEvent.next_attempt_at.is_(None),
                            WebhookEvent.next_attempt_at <= current,
                        ),
                    ),
                    and_(
                        WebhookEvent.status.in_(
                            ['processing', 'delivering']
                        ),
                        or_(
                            WebhookEvent.lease_until.is_(None),
                            WebhookEvent.lease_until <= current,
                        ),
                    ),
                )
            )
            .order_by(WebhookEvent.created_at.asc())
            .limit(max(1, min(limit, 1000)))
        )
    ).scalars().all()


async def request_manual_webhook_retry(
    db: AsyncSession,
    event_id,
) -> WebhookEvent | None:
    try:
        event_uuid = uuid.UUID(str(event_id))
    except (TypeError, ValueError):
        return None
    event = (
        await db.execute(
            select(WebhookEvent)
            .where(WebhookEvent.id == event_uuid)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not event:
        return None
    _normalize_legacy_status(event)
    if event.status == 'delivered':
        return event
    event.status = 'pending'
    event.max_attempts = max(
        event.max_attempts,
        event.attempts + settings.PLATFORM_WEBHOOK_RETRY_LIMIT,
    )
    event.next_attempt_at = None
    event.next_retry_at = None
    _release_lease(event)
    return event
