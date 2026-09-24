import argparse
import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.enums import Role
from app.core.security import encrypt_secret, hash_password
from app.db.session import AsyncSessionLocal
from app.models import User
from app.services.audit import audit


logger = logging.getLogger(__name__)


def _validate_configuration(*, confirm_production: bool) -> None:
    if settings.is_production and not confirm_production:
        raise RuntimeError(
            'production bootstrap requires --confirm-production'
        )
    if '@' not in settings.SUPERADMIN_EMAIL:
        raise RuntimeError('SUPERADMIN_EMAIL must be a valid configured email')
    if len(settings.SUPERADMIN_PASSWORD) < 12:
        raise RuntimeError('SUPERADMIN_PASSWORD does not meet the minimum length')
    if len(settings.SUPERADMIN_2FA_SECRET) < 16:
        raise RuntimeError('SUPERADMIN_2FA_SECRET is not configured')


async def bootstrap_superadmin(
    db: AsyncSession,
    *,
    update_existing: bool = False,
) -> str:
    email = settings.SUPERADMIN_EMAIL.strip().lower()
    target = (
        await db.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()

    if target is None:
        existing_superadmin = (
            await db.execute(
                select(User)
                .where(User.role == Role.superadmin.value)
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing_superadmin is not None:
            logger.warning(
                'superadmin_bootstrap_skipped',
                extra={
                    'event': 'superadmin_bootstrap_skipped',
                    'reason': 'superadmin_already_exists',
                },
            )
            return 'unchanged'

        target = User(
            email=email,
            password_hash=hash_password(settings.SUPERADMIN_PASSWORD),
            role=Role.superadmin.value,
            twofa_secret=encrypt_secret(settings.SUPERADMIN_2FA_SECRET),
            twofa_enabled=True,
        )
        db.add(target)
        await db.flush()
        await audit(
            db,
            'superadmin_bootstrap_created',
            'user',
            target.id,
            target.id,
            None,
            {'source': 'explicit_cli'},
        )
        logger.info(
            'superadmin_bootstrap_created',
            extra={'event': 'superadmin_bootstrap_created'},
        )
        return 'created'

    if not update_existing:
        logger.info(
            'superadmin_bootstrap_unchanged',
            extra={
                'event': 'superadmin_bootstrap_unchanged',
                'reason': 'configured_user_exists',
            },
        )
        return 'unchanged'

    target.password_hash = hash_password(settings.SUPERADMIN_PASSWORD)
    target.role = Role.superadmin.value
    target.twofa_secret = encrypt_secret(settings.SUPERADMIN_2FA_SECRET)
    target.twofa_enabled = True
    await audit(
        db,
        'superadmin_bootstrap_updated',
        'user',
        target.id,
        target.id,
        None,
        {
            'source': 'explicit_cli',
            'updated_fields': [
                'password_hash',
                'role',
                'twofa_secret',
                'twofa_enabled',
            ],
            'lock_state_preserved': True,
        },
    )
    logger.warning(
        'superadmin_bootstrap_updated',
        extra={'event': 'superadmin_bootstrap_updated'},
    )
    return 'updated'


async def _run(*, update_existing: bool, confirm_production: bool) -> str:
    _validate_configuration(confirm_production=confirm_production)
    async with AsyncSessionLocal() as db:
        result = await bootstrap_superadmin(
            db,
            update_existing=update_existing,
        )
        if result != 'unchanged':
            await db.commit()
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Explicitly create or update the configured Superadmin.'
    )
    parser.add_argument(
        '--update-existing',
        action='store_true',
        help='Explicitly update credentials and 2FA for the configured user.',
    )
    parser.add_argument(
        '--confirm-production',
        action='store_true',
        help='Confirm that this explicit command is intended for production.',
    )
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(
            _run(
                update_existing=args.update_existing,
                confirm_production=args.confirm_production,
            )
        )
    except Exception as exc:
        logger.error(
            'superadmin_bootstrap_failed',
            extra={
                'event': 'superadmin_bootstrap_failed',
                'error_type': exc.__class__.__name__,
            },
        )
        return 1
    print(f'superadmin_bootstrap result={result}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
