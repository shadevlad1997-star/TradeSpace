import asyncio
import hashlib
import ipaddress
import logging
import socket
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Awaitable, Callable
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import UUID

import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_secret, encrypt_secret, sign_hmac
from app.models import AIIntegrationConfig


AI_OFFICE_PROVIDER = 'veyra_ai_office'
AI_OFFICE_ENVIRONMENTS = {'local', 'staging', 'production'}
AI_OFFICE_AUTH_TYPES = {'none', 'bearer', 'api_key', 'hmac'}
AI_OFFICE_EVENT_OPTIONS = {
    'deposit.paid',
    'deposit.failed',
    'merchant_settlement.completed',
    'teamlead.accrual.created',
}
AI_OFFICE_SUCCESS_STATUSES = {'ok', 'healthy', 'ready'}
logger = logging.getLogger('app.integrations')


class AIIntegrationError(ValueError):
    pass


@dataclass(frozen=True)
class AIConnectionSnapshot:
    config_id: UUID
    provider: str
    environment: str
    base_url: str
    health_path: str
    api_version: str | None
    auth_type: str
    api_key: str
    bearer_token: str
    hmac_secret: str
    timeout_seconds: float
    connect_timeout_seconds: float
    max_retries: int
    verify_tls: bool


@dataclass(frozen=True)
class AIConnectionResult:
    success: bool
    status: str
    latency_ms: int
    error_code: str | None = None
    error_message_redacted: str | None = None


def _decimal_between(
    value: str | int | float | Decimal,
    *,
    minimum: Decimal,
    maximum: Decimal,
    code: str,
) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise AIIntegrationError(code) from exc
    if not parsed.is_finite() or parsed < minimum or parsed > maximum:
        raise AIIntegrationError(code)
    return parsed.quantize(Decimal('0.001'))


def _normalize_health_target(
    base_url: str,
    health_path: str,
) -> tuple[str, str, int]:
    clean_base = (base_url or '').strip().rstrip('/')
    clean_path = (health_path or '').strip()
    if not clean_base:
        raise AIIntegrationError('ai_office_base_url_required')
    if not clean_path.startswith('/') or clean_path.startswith('//'):
        raise AIIntegrationError('ai_office_health_path_invalid')
    if '://' in clean_path or '?' in clean_path or '#' in clean_path:
        raise AIIntegrationError('ai_office_health_path_invalid')
    if '..' in unquote(clean_path).split('/'):
        raise AIIntegrationError('ai_office_health_path_invalid')
    parsed = urlsplit(clean_base)
    if (
        parsed.scheme not in {'http', 'https'}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise AIIntegrationError('ai_office_base_url_invalid')
    if '..' in unquote(parsed.path).split('/'):
        raise AIIntegrationError('ai_office_base_url_invalid')
    try:
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    except ValueError as exc:
        raise AIIntegrationError('ai_office_base_url_invalid') from exc
    base_path = parsed.path.rstrip('/')
    target_path = f'{base_path}{clean_path}'
    netloc = (
        f'[{parsed.hostname}]'
        if ':' in parsed.hostname
        else parsed.hostname
    )
    if parsed.port:
        netloc = f'{netloc}:{parsed.port}'
    return (
        urlunsplit((parsed.scheme, netloc, target_path, '', '')),
        parsed.hostname,
        port,
    )


def _address_is_forbidden(address: str) -> bool:
    parsed = ipaddress.ip_address(address)
    return bool(
        parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_reserved
        or parsed.is_unspecified
    )


def _resolve_addresses(
    hostname: str,
    port: int,
    resolver: Callable = socket.getaddrinfo,
) -> set[str]:
    try:
        resolved = resolver(hostname, port, type=socket.SOCK_STREAM)
    except (OSError, socket.gaierror) as exc:
        raise AIIntegrationError('ai_office_dns_resolution_failed') from exc
    addresses = {
        str(item[4][0]).split('%', 1)[0]
        for item in resolved
        if item and len(item) >= 5 and item[4]
    }
    if not addresses:
        raise AIIntegrationError('ai_office_dns_resolution_failed')
    return addresses


def _validate_resolved_addresses(
    addresses: set[str],
    *,
    environment: str,
) -> None:
    if environment == 'local':
        return
    try:
        forbidden = any(_address_is_forbidden(address) for address in addresses)
    except ValueError as exc:
        raise AIIntegrationError('ai_office_dns_resolution_failed') from exc
    if forbidden:
        raise AIIntegrationError('ai_office_private_address_blocked')


def _required_secret_configured(
    auth_type: str,
    *,
    api_key: str | None,
    bearer_token: str | None,
    hmac_secret: str | None,
) -> bool:
    return {
        'none': True,
        'api_key': bool(api_key),
        'bearer': bool(bearer_token),
        'hmac': bool(hmac_secret),
    }.get(auth_type, False)


def validate_ai_configuration(
    *,
    enabled: bool,
    environment: str,
    base_url: str,
    health_path: str,
    auth_type: str,
    api_key: str | None,
    bearer_token: str | None,
    hmac_secret: str | None,
    timeout_seconds: Decimal,
    connect_timeout_seconds: Decimal,
    max_retries: int,
    verify_tls: bool,
) -> None:
    if environment not in AI_OFFICE_ENVIRONMENTS:
        raise AIIntegrationError('ai_office_environment_invalid')
    if auth_type not in AI_OFFICE_AUTH_TYPES:
        raise AIIntegrationError('ai_office_auth_type_invalid')
    if base_url:
        target_url, _, _ = _normalize_health_target(base_url, health_path)
        if environment == 'production' and not target_url.startswith('https://'):
            raise AIIntegrationError('ai_office_production_https_required')
    elif enabled:
        raise AIIntegrationError('ai_office_base_url_required')
    if environment == 'production' and enabled:
        raise AIIntegrationError('ai_office_production_activation_not_available')
    if environment == 'production' and not verify_tls:
        raise AIIntegrationError('ai_office_production_tls_required')
    if enabled and environment != 'local' and auth_type == 'none':
        raise AIIntegrationError('ai_office_m2m_auth_required')
    if enabled and not _required_secret_configured(
        auth_type,
        api_key=api_key,
        bearer_token=bearer_token,
        hmac_secret=hmac_secret,
    ):
        raise AIIntegrationError('ai_office_credential_required')
    if (
        timeout_seconds < Decimal('1')
        or timeout_seconds > Decimal('60')
        or connect_timeout_seconds < Decimal('0.1')
        or connect_timeout_seconds > timeout_seconds
    ):
        raise AIIntegrationError('ai_office_timeout_invalid')
    if max_retries < 0 or max_retries > 5:
        raise AIIntegrationError('ai_office_retries_invalid')


async def get_ai_integration_config(
    db: AsyncSession,
    *,
    for_update: bool = False,
) -> AIIntegrationConfig | None:
    statement = select(AIIntegrationConfig).where(
        AIIntegrationConfig.provider == AI_OFFICE_PROVIDER
    )
    if for_update:
        statement = statement.with_for_update()
    return (await db.execute(statement)).scalar_one_or_none()


def ai_config_view(config: AIIntegrationConfig | None) -> dict:
    if config is None:
        return {
            'exists': False,
            'provider': AI_OFFICE_PROVIDER,
            'enabled': False,
            'environment': 'local',
            'base_url': '',
            'health_path': '/api/health',
            'api_version': '',
            'auth_type': 'none',
            'api_key_configured': False,
            'bearer_token_configured': False,
            'hmac_secret_configured': False,
            'timeout_seconds': '5.000',
            'connect_timeout_seconds': '2.000',
            'max_retries': 0,
            'verify_tls': True,
            'selected_events': [],
            'inbound_commands_enabled': False,
            'last_connection_test_at': None,
            'last_connection_test_status': None,
            'last_connection_test_latency_ms': None,
            'last_success_at': None,
            'last_error_code': None,
            'last_error_message_redacted': None,
        }
    return {
        'exists': True,
        'provider': config.provider,
        'enabled': bool(config.enabled),
        'environment': config.environment,
        'base_url': config.base_url,
        'health_path': config.health_path,
        'api_version': config.api_version or '',
        'auth_type': config.auth_type,
        'api_key_configured': bool(config.encrypted_api_key),
        'bearer_token_configured': bool(config.encrypted_bearer_token),
        'hmac_secret_configured': bool(config.encrypted_hmac_secret),
        'timeout_seconds': str(config.timeout_seconds),
        'connect_timeout_seconds': str(config.connect_timeout_seconds),
        'max_retries': int(config.max_retries),
        'verify_tls': bool(config.verify_tls),
        'selected_events': list(config.selected_events or []),
        'inbound_commands_enabled': bool(config.inbound_commands_enabled),
        'last_connection_test_at': config.last_connection_test_at,
        'last_connection_test_status': config.last_connection_test_status,
        'last_connection_test_latency_ms': (
            config.last_connection_test_latency_ms
        ),
        'last_success_at': config.last_success_at,
        'last_error_code': config.last_error_code,
        'last_error_message_redacted': (
            config.last_error_message_redacted
        ),
    }


async def save_ai_integration_config(
    db: AsyncSession,
    *,
    actor_id: UUID,
    enabled: bool,
    environment: str,
    base_url: str,
    health_path: str,
    api_version: str | None,
    auth_type: str,
    api_key: str | None,
    bearer_token: str | None,
    hmac_secret: str | None,
    timeout_seconds: str | int | float | Decimal,
    connect_timeout_seconds: str | int | float | Decimal,
    max_retries: int,
    verify_tls: bool,
    selected_events: list[str] | tuple[str, ...],
) -> AIIntegrationConfig:
    await db.execute(
        text('SELECT pg_advisory_xact_lock(hashtext(:lock_key))'),
        {'lock_key': 'ai-integration-config:veyra-ai-office'},
    )
    config = await get_ai_integration_config(db, for_update=True)
    current_api_key = config.encrypted_api_key if config else None
    current_bearer = config.encrypted_bearer_token if config else None
    current_hmac = config.encrypted_hmac_secret if config else None
    clean_api_key = (api_key or '').strip()
    clean_bearer = (bearer_token or '').strip()
    clean_hmac = (hmac_secret or '').strip()
    next_api_key = encrypt_secret(clean_api_key) if clean_api_key else current_api_key
    next_bearer = encrypt_secret(clean_bearer) if clean_bearer else current_bearer
    next_hmac = encrypt_secret(clean_hmac) if clean_hmac else current_hmac
    clean_environment = (environment or '').strip().lower()
    clean_base_url = (base_url or '').strip().rstrip('/')[:500]
    clean_health_path = (health_path or '').strip()[:255] or '/api/health'
    clean_auth_type = (auth_type or '').strip().lower()
    parsed_timeout = _decimal_between(
        timeout_seconds,
        minimum=Decimal('1'),
        maximum=Decimal('60'),
        code='ai_office_timeout_invalid',
    )
    parsed_connect_timeout = _decimal_between(
        connect_timeout_seconds,
        minimum=Decimal('0.1'),
        maximum=parsed_timeout,
        code='ai_office_connect_timeout_invalid',
    )
    try:
        clean_retries = int(max_retries)
    except (TypeError, ValueError) as exc:
        raise AIIntegrationError('ai_office_retries_invalid') from exc
    clean_events = sorted(
        {
            str(item).strip()
            for item in selected_events
            if str(item).strip() in AI_OFFICE_EVENT_OPTIONS
        }
    )
    validate_ai_configuration(
        enabled=bool(enabled),
        environment=clean_environment,
        base_url=clean_base_url,
        health_path=clean_health_path,
        auth_type=clean_auth_type,
        api_key=next_api_key,
        bearer_token=next_bearer,
        hmac_secret=next_hmac,
        timeout_seconds=parsed_timeout,
        connect_timeout_seconds=parsed_connect_timeout,
        max_retries=clean_retries,
        verify_tls=bool(verify_tls),
    )
    if config is None:
        config = AIIntegrationConfig(
            provider=AI_OFFICE_PROVIDER,
            created_by=actor_id,
        )
        db.add(config)
    config.enabled = bool(enabled)
    config.environment = clean_environment
    config.base_url = clean_base_url
    config.health_path = clean_health_path
    config.api_version = (api_version or '').strip()[:64] or None
    config.auth_type = clean_auth_type
    config.encrypted_api_key = next_api_key
    config.encrypted_bearer_token = next_bearer
    config.encrypted_hmac_secret = next_hmac
    config.timeout_seconds = parsed_timeout
    config.connect_timeout_seconds = parsed_connect_timeout
    config.max_retries = clean_retries
    config.verify_tls = bool(verify_tls)
    config.selected_events = clean_events
    # Commands and background delivery are explicitly outside v2.0-rc2.
    config.inbound_commands_enabled = False
    config.updated_by = actor_id
    await db.flush()
    return config


async def clear_ai_integration_secret(
    db: AsyncSession,
    *,
    actor_id: UUID,
    secret_kind: str,
) -> AIIntegrationConfig:
    config = await get_ai_integration_config(db, for_update=True)
    if config is None:
        raise AIIntegrationError('ai_office_config_not_found')
    if config.enabled:
        raise AIIntegrationError(
            'ai_office_disable_before_clearing_credential'
        )
    field_by_kind = {
        'api_key': 'encrypted_api_key',
        'bearer_token': 'encrypted_bearer_token',
        'hmac_secret': 'encrypted_hmac_secret',
    }
    field = field_by_kind.get(secret_kind)
    if field is None:
        raise AIIntegrationError('ai_office_secret_kind_invalid')
    setattr(config, field, None)
    config.updated_by = actor_id
    await db.flush()
    return config


def ai_connection_snapshot(
    config: AIIntegrationConfig,
) -> AIConnectionSnapshot:
    return AIConnectionSnapshot(
        config_id=config.id,
        provider=config.provider,
        environment=config.environment,
        base_url=config.base_url,
        health_path=config.health_path,
        api_version=config.api_version,
        auth_type=config.auth_type,
        api_key=decrypt_secret(config.encrypted_api_key),
        bearer_token=decrypt_secret(config.encrypted_bearer_token),
        hmac_secret=decrypt_secret(config.encrypted_hmac_secret),
        timeout_seconds=float(config.timeout_seconds),
        connect_timeout_seconds=float(config.connect_timeout_seconds),
        max_retries=int(config.max_retries),
        verify_tls=bool(config.verify_tls),
    )


def _connection_headers(snapshot: AIConnectionSnapshot) -> dict[str, str]:
    headers = {'Accept': 'application/json'}
    if snapshot.api_version:
        headers['X-API-Version'] = snapshot.api_version
    if snapshot.auth_type == 'api_key':
        headers['X-API-Key'] = snapshot.api_key
    elif snapshot.auth_type == 'bearer':
        headers['Authorization'] = f'Bearer {snapshot.bearer_token}'
    elif snapshot.auth_type == 'hmac':
        timestamp = str(int(time.time()))
        headers['X-Timestamp'] = timestamp
        headers['X-Signature'] = sign_hmac(
            snapshot.hmac_secret,
            timestamp,
            b'',
        )
    return headers


def _has_ssl_cause(exc: BaseException) -> bool:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, ssl.SSLError):
            return True
        current = current.__cause__ or current.__context__
    return False


def _failure(
    code: str,
    *,
    latency_ms: int,
) -> AIConnectionResult:
    messages = {
        'ai_office_dns_resolution_failed': 'DNS resolution failed.',
        'ai_office_private_address_blocked': (
            'Private, loopback, or link-local target is not allowed.'
        ),
        'ai_office_timeout': 'Connection timed out.',
        'ai_office_tls_error': 'TLS verification failed.',
        'ai_office_connection_error': 'Connection failed.',
        'ai_office_unexpected_status': (
            'Health endpoint returned an unexpected HTTP status.'
        ),
        'ai_office_unexpected_content_type': (
            'Health endpoint did not return JSON.'
        ),
        'ai_office_unexpected_health_payload': (
            'Health endpoint returned an unexpected health payload.'
        ),
        'ai_office_configuration_invalid': (
            'Saved integration configuration is invalid.'
        ),
    }
    return AIConnectionResult(
        success=False,
        status='failed',
        latency_ms=max(0, latency_ms),
        error_code=code,
        error_message_redacted=messages.get(code, 'Connection test failed.'),
    )


async def perform_ai_connection_test(
    snapshot: AIConnectionSnapshot,
    *,
    resolver: Callable = socket.getaddrinfo,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AIConnectionResult:
    started = time.perf_counter()
    try:
        target_url, hostname, port = _normalize_health_target(
            snapshot.base_url,
            snapshot.health_path,
        )
        validate_ai_configuration(
            enabled=False,
            environment=snapshot.environment,
            base_url=snapshot.base_url,
            health_path=snapshot.health_path,
            auth_type=snapshot.auth_type,
            api_key=snapshot.api_key,
            bearer_token=snapshot.bearer_token,
            hmac_secret=snapshot.hmac_secret,
            timeout_seconds=Decimal(str(snapshot.timeout_seconds)),
            connect_timeout_seconds=Decimal(
                str(snapshot.connect_timeout_seconds)
            ),
            max_retries=snapshot.max_retries,
            verify_tls=snapshot.verify_tls,
        )
        if not _required_secret_configured(
            snapshot.auth_type,
            api_key=snapshot.api_key,
            bearer_token=snapshot.bearer_token,
            hmac_secret=snapshot.hmac_secret,
        ):
            raise AIIntegrationError('ai_office_credential_required')
        initial_addresses = await asyncio.to_thread(
            _resolve_addresses,
            hostname,
            port,
            resolver,
        )
        _validate_resolved_addresses(
            initial_addresses,
            environment=snapshot.environment,
        )
    except AIIntegrationError as exc:
        code = str(exc)
        if code not in {
            'ai_office_dns_resolution_failed',
            'ai_office_private_address_blocked',
        }:
            code = 'ai_office_configuration_invalid'
        return _failure(
            code,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    timeout = httpx.Timeout(
        snapshot.timeout_seconds,
        connect=snapshot.connect_timeout_seconds,
    )
    attempts = max(1, snapshot.max_retries + 1)
    for attempt in range(attempts):
        try:
            # Resolve again immediately before each GET to reduce DNS rebinding
            # exposure. No redirects are followed.
            final_addresses = await asyncio.to_thread(
                _resolve_addresses,
                hostname,
                port,
                resolver,
            )
            _validate_resolved_addresses(
                final_addresses,
                environment=snapshot.environment,
            )
            async with httpx.AsyncClient(
                timeout=timeout,
                verify=snapshot.verify_tls,
                follow_redirects=False,
                transport=transport,
            ) as client:
                response = await client.get(
                    target_url,
                    headers=_connection_headers(snapshot),
                )
        except AIIntegrationError as exc:
            return _failure(
                str(exc),
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        except httpx.TimeoutException:
            if attempt + 1 < attempts:
                continue
            return _failure(
                'ai_office_timeout',
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        except httpx.TransportError as exc:
            if attempt + 1 < attempts:
                continue
            return _failure(
                (
                    'ai_office_tls_error'
                    if _has_ssl_cause(exc)
                    else 'ai_office_connection_error'
                ),
                latency_ms=int((time.perf_counter() - started) * 1000),
            )

        latency_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code < 200 or response.status_code >= 300:
            return _failure(
                'ai_office_unexpected_status',
                latency_ms=latency_ms,
            )
        content_type = response.headers.get('content-type', '').lower()
        if 'application/json' not in content_type:
            return _failure(
                'ai_office_unexpected_content_type',
                latency_ms=latency_ms,
            )
        try:
            payload = response.json()
        except ValueError:
            return _failure(
                'ai_office_unexpected_health_payload',
                latency_ms=latency_ms,
            )
        status_value = (
            str(payload.get('status') or '').strip().lower()
            if isinstance(payload, dict)
            else ''
        )
        is_expected = bool(
            isinstance(payload, dict)
            and (
                status_value in AI_OFFICE_SUCCESS_STATUSES
                or payload.get('ok') is True
            )
        )
        if not is_expected:
            return _failure(
                'ai_office_unexpected_health_payload',
                latency_ms=latency_ms,
            )
        return AIConnectionResult(
            success=True,
            status='success',
            latency_ms=latency_ms,
        )

    return _failure(
        'ai_office_connection_error',
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


async def run_ai_connection_test_without_transaction(
    db: AsyncSession,
    snapshot: AIConnectionSnapshot,
    *,
    runner: Callable[
        [AIConnectionSnapshot],
        Awaitable[AIConnectionResult],
    ]
    | None = None,
) -> AIConnectionResult:
    await db.rollback()
    if db.in_transaction():
        raise RuntimeError('DB transaction must be closed before HTTP')
    return await (runner or perform_ai_connection_test)(snapshot)


def record_ai_connection_result(
    config: AIIntegrationConfig,
    result: AIConnectionResult,
    *,
    tested_at: datetime | None = None,
) -> None:
    now = tested_at or datetime.now(timezone.utc)
    config.last_connection_test_at = now
    config.last_connection_test_status = result.status
    config.last_connection_test_latency_ms = result.latency_ms
    if result.success:
        config.last_success_at = now
        config.last_error_code = None
        config.last_error_message_redacted = None
    else:
        config.last_error_code = result.error_code
        config.last_error_message_redacted = result.error_message_redacted
        logger.warning(
            'ai_office_connection_test_failed',
            extra={
                'provider': config.provider,
                'environment': config.environment,
                'error_code': result.error_code,
            },
        )
