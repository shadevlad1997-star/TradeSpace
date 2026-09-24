"""Read-only TradeSpace v2.0 release preflight.

This script never applies migrations or writes business/database records.
It intentionally reports check names and redacted error codes only.
"""

import argparse
import asyncio
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from cryptography.fernet import Fernet
from redis.asyncio import Redis
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import is_encrypted_value
from app.models import AIIntegrationConfig, PlatformCryptoWallet
from app.services.ai_office import validate_ai_configuration
from app.services.platform_wallet import normalize_platform_wallet_address
from app.services.rapira import (
    RollingRateUnavailable,
    get_strict_rolling_ask_quote,
)


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


def _check(name: str, passed: bool, detail: str) -> Check:
    return Check(name=name, passed=bool(passed), detail=detail)


def _safe_error_code(exc: BaseException) -> str:
    known = str(exc).strip()
    if known and len(known) <= 80 and all(
        character.isalnum() or character in {'_', '-', ' '}
        for character in known
    ):
        return known.replace(' ', '_').lower()
    return type(exc).__name__.lower()


def check_environment() -> list[Check]:
    environment = settings.ENV.strip().lower()
    return [
        _check(
            'APP_VERSION',
            settings.APP_VERSION == '2.0.0-rc2',
            settings.APP_VERSION,
        ),
        _check(
            'APP_ENV',
            environment in {'local', 'test', 'staging', 'production', 'prod'},
            f'configured ({environment or "missing"})',
        ),
        _check(
            'DATABASE_URL',
            bool(settings.DATABASE_URL and settings.SYNC_DATABASE_URL),
            'configured (value redacted)',
        ),
        _check(
            'production_debug',
            not settings.is_production or not settings.DEBUG,
            'disabled' if not settings.DEBUG else 'must be disabled',
        ),
    ]


def check_required_secrets() -> list[Check]:
    encryption_valid = False
    try:
        Fernet(settings.ENCRYPTION_KEY.encode('utf-8'))
        encryption_valid = True
    except Exception:
        pass
    return [
        _check(
            'session_hmac_secret',
            len(settings.SECRET_KEY.strip()) >= 32,
            'configured (value redacted)',
        ),
        _check(
            'encryption_secret',
            encryption_valid,
            'configured (value redacted)',
        ),
        _check(
            'superadmin_2fa_seed',
            len(settings.SUPERADMIN_2FA_SECRET.strip()) >= 16,
            'configured (value redacted)',
        ),
    ]


def check_migrations_and_database() -> tuple[list[Check], object | None]:
    checks: list[Check] = []
    engine = None
    try:
        alembic_config = Config(str(ROOT / 'alembic.ini'))
        script = ScriptDirectory.from_config(alembic_config)
        heads = tuple(script.get_heads())
        checks.append(
            _check(
                'alembic_single_head',
                len(heads) == 1,
                f'{len(heads)} head(s)',
            )
        )
        engine = create_engine(settings.SYNC_DATABASE_URL, pool_pre_ping=True)
        with engine.connect() as connection:
            connection.execute(text('SELECT 1'))
            context = MigrationContext.configure(connection)
            current_heads = tuple(context.get_current_heads())
            checks.append(_check('database_connectivity', True, 'ok'))
            checks.append(
                _check(
                    'migrations_current',
                    len(heads) == 1 and current_heads == heads,
                    (
                        f'current={",".join(current_heads) or "none"} '
                        f'head={",".join(heads) or "none"}'
                    ),
                )
            )
    except Exception as exc:
        checks.append(
            _check(
                'database_and_migrations',
                False,
                f'failed ({_safe_error_code(exc)})',
            )
        )
    return checks, engine


def check_platform_wallet(engine) -> list[Check]:
    if engine is None:
        return [_check('platform_wallet_count', False, 'database unavailable')]
    try:
        with Session(engine) as session:
            count = session.scalar(
                select(func.count(PlatformCryptoWallet.id)).where(
                    PlatformCryptoWallet.is_active.is_(True)
                )
            )
            active = session.scalar(
                select(PlatformCryptoWallet).where(
                    PlatformCryptoWallet.is_active.is_(True)
                )
            )
        valid = True
        if active is not None:
            normalize_platform_wallet_address(active.address)
        return [
            _check(
                'platform_wallet_count',
                int(count or 0) <= 1,
                f'{int(count or 0)} active',
            ),
            _check(
                'platform_wallet_address',
                valid,
                'valid' if active is not None else 'not configured',
            ),
        ]
    except Exception as exc:
        return [
            _check(
                'platform_wallet',
                False,
                f'failed ({_safe_error_code(exc)})',
            )
        ]


def check_ai_office(engine) -> list[Check]:
    if engine is None:
        return [_check('ai_office_config', False, 'database unavailable')]
    try:
        with Session(engine) as session:
            config = session.scalar(
                select(AIIntegrationConfig).where(
                    AIIntegrationConfig.provider == 'veyra_ai_office'
                )
            )
        if config is None:
            return [_check('ai_office_config', True, 'disabled (not configured)')]
        encrypted = all(
            not value or is_encrypted_value(value)
            for value in (
                config.encrypted_api_key,
                config.encrypted_bearer_token,
                config.encrypted_hmac_secret,
            )
        )
        validate_ai_configuration(
            enabled=bool(config.enabled),
            environment=config.environment,
            base_url=config.base_url,
            health_path=config.health_path,
            auth_type=config.auth_type,
            api_key=config.encrypted_api_key,
            bearer_token=config.encrypted_bearer_token,
            hmac_secret=config.encrypted_hmac_secret,
            timeout_seconds=config.timeout_seconds,
            connect_timeout_seconds=config.connect_timeout_seconds,
            max_retries=config.max_retries,
            verify_tls=config.verify_tls,
        )
        return [
            _check(
                'ai_office_config',
                encrypted,
                (
                    'enabled with valid config'
                    if config.enabled
                    else 'disabled with valid config'
                ),
            ),
            _check(
                'ai_office_inbound_commands',
                not config.inbound_commands_enabled,
                'disabled',
            ),
        ]
    except Exception as exc:
        return [
            _check(
                'ai_office_config',
                False,
                f'invalid ({_safe_error_code(exc)})',
            )
        ]


def check_required_directories() -> list[Check]:
    paths = (ROOT / 'uploads', Path(tempfile.gettempdir()))
    return [
        _check(
            f'writable_directory:{path.name or "temp"}',
            path.exists() and path.is_dir() and os.access(path, os.W_OK),
            'writable' if path.exists() and os.access(path, os.W_OK) else 'not writable',
        )
        for path in paths
    ]


async def check_redis() -> Check:
    client = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        return _check('redis_connectivity', bool(await client.ping()), 'ok')
    except Exception as exc:
        return _check(
            'redis_connectivity',
            False,
            f'failed ({_safe_error_code(exc)})',
        )
    finally:
        await client.aclose()


async def check_rapira() -> Check:
    if not settings.RAPIRA_RATES_ENABLED:
        return _check('rapira_live_ask', False, 'disabled')
    try:
        quote = await get_strict_rolling_ask_quote()
        return _check(
            'rapira_live_ask',
            bool(quote.rate > 0 and quote.side == 'ask'),
            (
                f'ok symbol={quote.symbol} source={quote.source} '
                f'freshness={quote.freshness_basis}'
            ),
        )
    except RollingRateUnavailable:
        return _check('rapira_live_ask', False, 'unavailable')
    except Exception as exc:
        return _check(
            'rapira_live_ask',
            False,
            f'failed ({_safe_error_code(exc)})',
        )


async def check_health(base_url: str) -> list[Check]:
    checks = []
    async with httpx.AsyncClient(
        base_url=base_url.rstrip('/'),
        timeout=5,
        follow_redirects=False,
    ) as client:
        for path, expected_status in (('/health', 'ok'), ('/ready', 'ready')):
            try:
                response = await client.get(path)
                payload = response.json()
                checks.append(
                    _check(
                        f'endpoint:{path}',
                        (
                            response.status_code == 200
                            and isinstance(payload, dict)
                            and payload.get('status') == expected_status
                        ),
                        f'HTTP {response.status_code}',
                    )
                )
            except Exception as exc:
                checks.append(
                    _check(
                        f'endpoint:{path}',
                        False,
                        f'failed ({_safe_error_code(exc)})',
                    )
                )
    return checks


async def run_preflight(base_url: str) -> list[Check]:
    checks = [
        *check_environment(),
        *check_required_secrets(),
    ]
    migration_checks, engine = check_migrations_and_database()
    checks.extend(migration_checks)
    checks.extend(check_platform_wallet(engine))
    checks.extend(check_ai_office(engine))
    checks.extend(check_required_directories())
    checks.append(await check_redis())
    checks.append(await check_rapira())
    checks.extend(await check_health(base_url))
    if engine is not None:
        engine.dispose()
    return checks


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Read-only TradeSpace v2.0 release preflight'
    )
    parser.add_argument(
        '--base-url',
        default='http://localhost:8000',
        help='Running local API base URL',
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    checks = asyncio.run(run_preflight(args.base_url))
    for item in checks:
        status = 'PASS' if item.passed else 'FAIL'
        print(f'{status} {item.name}: {item.detail}')
    passed = sum(1 for item in checks if item.passed)
    print(f'RESULT {passed}/{len(checks)} checks passed')
    return 0 if passed == len(checks) else 1


if __name__ == '__main__':
    raise SystemExit(main())
