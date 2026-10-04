"""Local draft CRUD only: no credentials, provider client or payment commands."""
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select

from app.core.russian_banks import find_bank, find_bank_by_code
from app.models import Requisite, TraderBankConnection
from app.services.audit import audit

BANK_CODES = ("alfabank", "modulbank")


class DraftValidationError(ValueError):
    pass


def require_trader(user):
    # QR-2.0 is granted to Trader only, not other roles sharing the cabinet.
    if user.role != "trader":
        raise HTTPException(403, "Нет доступа")


def parse_id(value):
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(404, "Черновик не найден") from None


async def owned_draft(db, user, draft_id, *, lock=False):
    require_trader(user)
    query = select(TraderBankConnection).where(
        TraderBankConnection.id == parse_id(draft_id), TraderBankConnection.trader_id == user.id,
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    row = await db.scalar(query)
    if row is None:
        raise HTTPException(404, "Черновик не найден")
    return row


async def _validated_requisite(db, user, bank_code, value):
    if not value:
        return None
    try:
        requisite_id = UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise DraftValidationError("Выберите свой реквизит QR NSPK для выбранного банка.") from None
    req = await db.scalar(select(Requisite).where(
        Requisite.id == requisite_id, Requisite.trader_id == user.id,
        Requisite.method == "sbp_qr", Requisite.is_archived.is_(False),
        Requisite.status != "deleted",
    ).with_for_update().execution_options(populate_existing=True))
    bank = find_bank(req.bank_code or req.bank_name) if req else None
    if not bank or not bank.enabled or bank.code != bank_code:
        raise DraftValidationError("Выберите свой реквизит QR NSPK для выбранного банка.")
    return req.id


async def save_draft(db, user, *, bank_code, name, requisite_id=None, draft_id=None, ip=None):
    require_trader(user)
    row = await owned_draft(db, user, draft_id, lock=True) if draft_id else None
    bank = find_bank_by_code(bank_code)
    if bank_code not in BANK_CODES or not bank or not bank.enabled:
        raise DraftValidationError("Выберите Альфа-Банк или Модульбанк.")
    clean_name = name.strip()
    if not clean_name or len(clean_name) > 120 or any(ord(c) < 32 for c in clean_name):
        raise DraftValidationError("Укажите название подключения: от 1 до 120 символов.")
    linked_id = await _validated_requisite(db, user, bank_code, requisite_id)
    if row is None:
        row = TraderBankConnection(trader_id=user.id)
        db.add(row)
    row.bank_code, row.name, row.requisite_id = bank_code, clean_name, linked_id
    await db.flush()
    await audit(db, "bank_draft_updated" if draft_id else "bank_draft_created", "bank_connection",
                actor_id=user.id, target_id=row.id, ip=ip,
                details={"bank_code": bank_code, "requisite_id": str(linked_id) if linked_id else None})
    return row


async def delete_draft(db, user, draft_id, *, ip=None):
    row = await owned_draft(db, user, draft_id, lock=True)
    await audit(db, "bank_draft_deleted", "bank_connection", actor_id=user.id, target_id=row.id,
                ip=ip, details={"bank_code": row.bank_code})
    await db.delete(row)
