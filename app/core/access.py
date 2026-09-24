from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import Role
from app.models import Appeal, Deposit, Merchant, Payout, Requisite, User


STAFF_ROLES = {Role.superadmin.value, Role.admin.value, Role.support.value}
TRADER_ROLES = {Role.operator.value, Role.trader.value}


def _role_value(user: User | Any) -> str:
    role = getattr(user, "role", "")
    return getattr(role, "value", role)


def is_staff(user: User | Any) -> bool:
    return _role_value(user) in STAFF_ROLES


def is_trader_role(user: User | Any) -> bool:
    return _role_value(user) in TRADER_ROLES


def can_view_admin_stats(user: User | Any) -> bool:
    return is_staff(user)


def _appeal_metadata(appeal: Appeal | Any) -> dict:
    value = getattr(appeal, "metadata_json", None)
    return value if isinstance(value, dict) else {}


async def _merchant_for_user(db: AsyncSession, user: User | Any) -> Merchant | None:
    return (
        await db.execute(select(Merchant).where(Merchant.owner_id == getattr(user, "id", None)))
    ).scalar_one_or_none()


async def _deposit_for_appeal(db: AsyncSession, appeal: Appeal | Any) -> Deposit | None:
    return (
        await db.execute(select(Deposit).where(Deposit.id == getattr(appeal, "operation_id", None)))
    ).scalar_one_or_none()


async def _payout_for_appeal(db: AsyncSession, appeal: Appeal | Any) -> Payout | None:
    return (
        await db.execute(select(Payout).where(Payout.id == getattr(appeal, "operation_id", None)))
    ).scalar_one_or_none()


async def _deposit_requisite_trader_id(db: AsyncSession, deposit: Deposit | Any) -> str | None:
    requisites_id = getattr(deposit, "requisites_id", None)
    if not requisites_id:
        return None
    requisite = (
        await db.execute(select(Requisite).where(Requisite.id == requisites_id))
    ).scalar_one_or_none()
    trader_id = getattr(requisite, "trader_id", None)
    return str(trader_id) if trader_id else None


async def _merchant_can_view_deposit(db: AsyncSession, user: User | Any, deposit: Deposit | Any) -> bool:
    merchant = await _merchant_for_user(db, user)
    return bool(merchant and getattr(deposit, "merchant_id", None) == merchant.id)


async def _merchant_can_view_payout(db: AsyncSession, user: User | Any, payout: Payout | Any) -> bool:
    merchant = await _merchant_for_user(db, user)
    return bool(merchant and getattr(payout, "merchant_id", None) == merchant.id)


async def _trader_can_view_deposit(db: AsyncSession, user: User | Any, appeal: Appeal | Any, deposit: Deposit | Any) -> bool:
    user_id = str(getattr(user, "id", ""))
    metadata = _appeal_metadata(appeal)
    if str(metadata.get("trader_id") or "") == user_id:
        return True
    return str(await _deposit_requisite_trader_id(db, deposit) or "") == user_id


async def can_view_appeal(db: AsyncSession, user: User | Any, appeal: Appeal | Any) -> bool:
    role = _role_value(user)
    if is_staff(user):
        return True

    operation_type = getattr(appeal, "operation_type", None)
    if operation_type == "deposit":
        deposit = await _deposit_for_appeal(db, appeal)
        if not deposit:
            return False
        if role == Role.merchant.value:
            return await _merchant_can_view_deposit(db, user, deposit)
        if is_trader_role(user):
            return await _trader_can_view_deposit(db, user, appeal, deposit)
        return False

    if operation_type == "payout":
        payout = await _payout_for_appeal(db, appeal)
        if not payout:
            return False
        if role == Role.merchant.value:
            return await _merchant_can_view_payout(db, user, payout)
        return False

    return False


async def can_message_appeal(db: AsyncSession, user: User | Any, appeal: Appeal | Any) -> bool:
    return await can_view_appeal(db, user, appeal)
