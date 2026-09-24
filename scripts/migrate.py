import argparse
import re
from pathlib import Path
from urllib.parse import urlsplit

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from app.core.config import settings


ROOT = Path(__file__).resolve().parents[1]


def _config() -> Config:
    config = Config(str(ROOT / 'alembic.ini'))
    config.set_main_option('sqlalchemy.url', settings.SYNC_DATABASE_URL)
    return config


def _revision_state(config: Config) -> tuple[tuple[str, ...], tuple[str, ...]]:
    script = ScriptDirectory.from_config(config)
    heads = tuple(script.get_heads())
    engine = create_engine(settings.SYNC_DATABASE_URL, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            current = tuple(
                revision
                for revision in MigrationContext.configure(
                    connection
                ).get_current_heads()
                if revision
            )
    finally:
        engine.dispose()
    return current, heads


def _format_revisions(revisions: tuple[str, ...]) -> str:
    return ','.join(revisions) if revisions else 'base'


def _safe_error(exc: Exception) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    secrets = {
        settings.DATABASE_URL,
        settings.SYNC_DATABASE_URL,
    }
    for value in (settings.DATABASE_URL, settings.SYNC_DATABASE_URL):
        parsed = urlsplit(value)
        if parsed.password:
            secrets.add(parsed.password)
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            message = message.replace(secret, '***redacted***')
    return re.sub(
        r'(?i)(postgres(?:ql)?(?:\+\w+)?://)[^\s/@]+(?::[^\s/@]*)?@',
        r'\1***redacted***@',
        message,
    )


def run(*, preflight: bool = False) -> int:
    config = _config()
    try:
        current, heads = _revision_state(config)
        if len(heads) != 1:
            raise RuntimeError(
                f'expected one Alembic head, found {_format_revisions(heads)}'
            )
        print(
            'migration_preflight '
            f'current={_format_revisions(current)} '
            f'head={_format_revisions(heads)}'
        )
        if preflight:
            print('migration_preflight_ok no_changes=true')
            return 0

        command.upgrade(config, 'head')
        upgraded, expected_heads = _revision_state(config)
        if upgraded != expected_heads:
            raise RuntimeError(
                'Alembic upgrade completed without reaching the expected head'
            )
        print(
            'migration_complete '
            f'current={_format_revisions(upgraded)} '
            f'head={_format_revisions(expected_heads)}'
        )
        return 0
    except Exception as exc:
        print(
            f'migration_failed error_type={exc.__class__.__name__} '
            f'error={_safe_error(exc)}'
        )
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Run the explicit TradeSpace Alembic release job.'
    )
    parser.add_argument(
        '--preflight',
        '--dry-run',
        dest='preflight',
        action='store_true',
        help='Check database connectivity and report current/head without writes.',
    )
    args = parser.parse_args(argv)
    return run(preflight=args.preflight)


if __name__ == '__main__':
    raise SystemExit(main())
