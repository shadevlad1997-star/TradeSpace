import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import decrypt_secret, encrypt_secret
from app.models import Merchant, MerchantWebhookSigningKey


class WebhookSigningKeyError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def webhook_key_fingerprint(key_id: str) -> str:
    return key_id[-8:] if len(key_id) > 8 else key_id


async def active_webhook_signing_key(
    db: AsyncSession,
    merchant_id,
    *,
    for_update: bool = False,
) -> MerchantWebhookSigningKey | None:
    statement = (
        select(MerchantWebhookSigningKey)
        .where(
            MerchantWebhookSigningKey.merchant_id == merchant_id,
            MerchantWebhookSigningKey.status == 'active',
            MerchantWebhookSigningKey.revoked_at.is_(None),
        )
        .order_by(MerchantWebhookSigningKey.created_at.desc())
        .limit(1)
    )
    if for_update:
        statement = statement.with_for_update()
    return (await db.execute(statement)).scalar_one_or_none()


async def issue_webhook_signing_key(
    db: AsyncSession,
    merchant: Merchant,
    *,
    created_by,
) -> tuple[MerchantWebhookSigningKey, str]:
    if await active_webhook_signing_key(
        db,
        merchant.id,
        for_update=True,
    ):
        raise WebhookSigningKeyError(
            'active_webhook_signing_key_exists',
            'An active webhook signing key already exists; rotate it instead.',
        )
    secret = 'whsec_' + secrets.token_urlsafe(32)
    key = MerchantWebhookSigningKey(
        merchant_id=merchant.id,
        key_id='whk_' + secrets.token_urlsafe(18),
        encrypted_secret=encrypt_secret(secret),
        status='active',
        created_by=created_by,
    )
    db.add(key)
    await db.flush()
    return key, secret


async def rotate_webhook_signing_key(
    db: AsyncSession,
    merchant: Merchant,
    *,
    created_by,
    overlap_seconds: int | None = None,
) -> tuple[MerchantWebhookSigningKey, str, MerchantWebhookSigningKey]:
    current = await active_webhook_signing_key(
        db,
        merchant.id,
        for_update=True,
    )
    if not current:
        raise WebhookSigningKeyError(
            'active_webhook_signing_key_missing',
            'No active webhook signing key exists; issue one first.',
        )
    overlap = (
        settings.WEBHOOK_SIGNING_KEY_OVERLAP_SECONDS
        if overlap_seconds is None
        else max(60, int(overlap_seconds))
    )
    current.status = 'retiring'
    current.retire_at = datetime.now(timezone.utc) + timedelta(
        seconds=overlap
    )
    new_key, secret = await issue_webhook_signing_key(
        db,
        merchant,
        created_by=created_by,
    )
    return new_key, secret, current


async def revoke_webhook_signing_key(
    db: AsyncSession,
    key: MerchantWebhookSigningKey,
) -> None:
    if key.status == 'revoked':
        raise WebhookSigningKeyError(
            'webhook_signing_key_already_revoked',
            'Webhook signing key is already revoked.',
        )
    key.status = 'revoked'
    key.revoked_at = datetime.now(timezone.utc)
    key.retire_at = None
    await db.flush()


async def delivery_webhook_signing_key(
    db: AsyncSession,
    merchant_id,
    preferred_key_id=None,
) -> tuple[MerchantWebhookSigningKey, str] | None:
    now = datetime.now(timezone.utc)
    if preferred_key_id:
        preferred = (
            await db.execute(
                select(MerchantWebhookSigningKey).where(
                    MerchantWebhookSigningKey.id == preferred_key_id,
                    MerchantWebhookSigningKey.merchant_id == merchant_id,
                    MerchantWebhookSigningKey.revoked_at.is_(None),
                    or_(
                        MerchantWebhookSigningKey.status == 'active',
                        (
                            MerchantWebhookSigningKey.status == 'retiring'
                        )
                        & (MerchantWebhookSigningKey.retire_at > now),
                    ),
                )
            )
        ).scalar_one_or_none()
        if preferred:
            secret = decrypt_secret(preferred.encrypted_secret)
            if secret:
                return preferred, secret

    active = await active_webhook_signing_key(db, merchant_id)
    if not active:
        return None
    secret = decrypt_secret(active.encrypted_secret)
    if not secret:
        return None
    return active, secret


async def verification_webhook_signing_keys(
    db: AsyncSession,
    merchant_id,
) -> list[MerchantWebhookSigningKey]:
    now = datetime.now(timezone.utc)
    return (
        await db.execute(
            select(MerchantWebhookSigningKey)
            .where(
                MerchantWebhookSigningKey.merchant_id == merchant_id,
                MerchantWebhookSigningKey.revoked_at.is_(None),
                or_(
                    MerchantWebhookSigningKey.status == 'active',
                    (
                        MerchantWebhookSigningKey.status == 'retiring'
                    )
                    & (MerchantWebhookSigningKey.retire_at > now),
                ),
            )
            .order_by(MerchantWebhookSigningKey.created_at.desc())
        )
    ).scalars().all()
