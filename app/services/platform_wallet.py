import io
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

import qrcode
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tron import TronAddressError, validate_trc20_address
from app.models import PlatformCryptoWallet


PLATFORM_WALLET_ASSET = 'USDT'
PLATFORM_WALLET_NETWORK = 'TRC20'


class PlatformWalletError(ValueError):
    pass


@dataclass(frozen=True)
class PlatformWalletChange:
    wallet: PlatformCryptoWallet
    previous_wallet_id: UUID | None
    changed: bool


def normalize_platform_wallet_address(address: str) -> str:
    try:
        return validate_trc20_address(address)
    except TronAddressError as exc:
        raise PlatformWalletError(str(exc)) from exc


def platform_wallet_qr_payload(wallet: PlatformCryptoWallet) -> str:
    return normalize_platform_wallet_address(wallet.address)


def platform_wallet_qr_png(wallet: PlatformCryptoWallet) -> bytes:
    image = qrcode.make(
        platform_wallet_qr_payload(wallet),
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        border=4,
    )
    output = io.BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()


async def get_active_platform_wallet(
    db: AsyncSession,
    *,
    for_update: bool = False,
) -> PlatformCryptoWallet | None:
    statement = select(PlatformCryptoWallet).where(
        PlatformCryptoWallet.asset == PLATFORM_WALLET_ASSET,
        PlatformCryptoWallet.network == PLATFORM_WALLET_NETWORK,
        PlatformCryptoWallet.is_active.is_(True),
    )
    if for_update:
        statement = statement.with_for_update()
    return (await db.execute(statement)).scalar_one_or_none()


async def list_platform_wallet_history(
    db: AsyncSession,
    *,
    limit: int = 100,
) -> list[PlatformCryptoWallet]:
    return list(
        (
            await db.execute(
                select(PlatformCryptoWallet)
                .order_by(
                    PlatformCryptoWallet.version.desc(),
                    PlatformCryptoWallet.created_at.desc(),
                )
                .limit(max(1, min(limit, 500)))
            )
        )
        .scalars()
        .all()
    )


async def set_active_platform_wallet(
    db: AsyncSession,
    *,
    address: str,
    label: str | None,
    actor_id: UUID,
    change_reason: str,
    now: datetime | None = None,
) -> PlatformWalletChange:
    normalized_address = normalize_platform_wallet_address(address)
    normalized_label = (label or '').strip()[:160] or None
    normalized_reason = (change_reason or '').strip()
    if not normalized_reason:
        raise PlatformWalletError('platform_wallet_change_reason_required')
    normalized_reason = normalized_reason[:500]
    changed_at = now or datetime.now(timezone.utc)

    # A transaction-scoped advisory lock serializes both first creation and
    # replacement. The partial unique index remains the final DB guarantee.
    await db.execute(
        text('SELECT pg_advisory_xact_lock(hashtext(:lock_key))'),
        {'lock_key': 'platform-wallet:USDT:TRC20'},
    )
    current = await get_active_platform_wallet(db, for_update=True)
    if (
        current
        and current.address == normalized_address
        and current.label == normalized_label
    ):
        return PlatformWalletChange(
            wallet=current,
            previous_wallet_id=None,
            changed=False,
        )

    previous_wallet_id = current.id if current else None
    if current:
        current.is_active = False
        current.deactivated_by = actor_id
        current.deactivated_at = changed_at

    latest_version = await db.scalar(
        select(func.max(PlatformCryptoWallet.version))
    )
    wallet = PlatformCryptoWallet(
        asset=PLATFORM_WALLET_ASSET,
        network=PLATFORM_WALLET_NETWORK,
        address=normalized_address,
        label=normalized_label,
        is_active=True,
        version=int(latest_version or 0) + 1,
        created_by=actor_id,
        change_reason=normalized_reason,
    )
    db.add(wallet)
    await db.flush()
    return PlatformWalletChange(
        wallet=wallet,
        previous_wallet_id=previous_wallet_id,
        changed=True,
    )
