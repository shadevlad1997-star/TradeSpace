import hashlib
import secrets
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.enums import Role
from app.core.security import encrypt_secret
from app.models import ApiKey, Merchant


SANDBOX_MODE = 'sandbox'
PRODUCTION_MODE = 'production'
API_KEY_MODES = (SANDBOX_MODE, PRODUCTION_MODE)
API_KEY_MANAGER_ROLES = {Role.superadmin.value, Role.admin.value}


class MerchantApiKeyError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def normalize_api_key_mode(value: str) -> str:
    mode = (value or '').strip().lower()
    aliases = {'live': PRODUCTION_MODE, 'prod': PRODUCTION_MODE, 'test': SANDBOX_MODE}
    mode = aliases.get(mode, mode)
    if mode not in API_KEY_MODES:
        raise MerchantApiKeyError('invalid_api_key_mode', 'API key mode must be sandbox or production.')
    return mode


def api_key_mode_label(mode: str) -> str:
    return 'Production' if normalize_api_key_mode(mode) == PRODUCTION_MODE else 'Sandbox'


def api_key_fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode('utf-8')).hexdigest()[:16]


def can_manage_merchant_api_keys(role: str) -> bool:
    return role in API_KEY_MANAGER_ROLES


def sandbox_key_allowed_for_merchant(merchant: Merchant) -> bool:
    if not settings.is_production:
        return True
    return settings.ALLOW_SANDBOX_KEYS_IN_PRODUCTION and bool(merchant.sandbox_mode)


async def issue_merchant_api_key(
    db: AsyncSession,
    merchant: Merchant,
    mode: str,
) -> tuple[ApiKey, str]:
    normalized_mode = normalize_api_key_mode(mode)
    existing = (await db.execute(
        select(ApiKey).where(
            ApiKey.merchant_id == merchant.id,
            ApiKey.mode == normalized_mode,
            ApiKey.is_active.is_(True),
        ).with_for_update()
    )).scalar_one_or_none()
    if existing:
        raise MerchantApiKeyError(
            'active_api_key_already_exists',
            f'Active {api_key_mode_label(normalized_mode)} API key already exists. Rotate that key instead.',
        )

    key_prefix = 'pk_test_' if normalized_mode == SANDBOX_MODE else 'pk_live_'
    api_key = key_prefix + secrets.token_urlsafe(24)
    secret_key = 'sk_' + secrets.token_urlsafe(32)
    key = ApiKey(
        merchant_id=merchant.id,
        api_key=api_key,
        secret_hash=encrypt_secret(secret_key),
        mode=normalized_mode,
        is_active=True,
    )
    db.add(key)
    await db.flush()
    return key, secret_key


async def revoke_merchant_api_key(db: AsyncSession, key: ApiKey) -> None:
    if not key.is_active:
        raise MerchantApiKeyError('api_key_already_revoked', 'API key is already revoked.')
    key.is_active = False
    key.revoked_at = datetime.now(timezone.utc)
    await db.flush()


async def rotate_merchant_api_key(
    db: AsyncSession,
    merchant: Merchant,
    key: ApiKey,
) -> tuple[ApiKey, str]:
    if key.merchant_id != merchant.id:
        raise MerchantApiKeyError('api_key_not_found', 'API key was not found for this merchant.')
    mode = normalize_api_key_mode(key.mode)
    await revoke_merchant_api_key(db, key)
    return await issue_merchant_api_key(db, merchant, mode)
