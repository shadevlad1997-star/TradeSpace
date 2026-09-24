from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Iterable

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import AppealStatus, DepositStatus, PaymentMethod
from app.models import Appeal, Deposit


DEPOSIT_ACTIVE_STATUSES = frozenset(
    {
        DepositStatus.created.value,
        DepositStatus.pending.value,
        DepositStatus.appeal_opened.value,
    }
)
DEPOSIT_TERMINAL_STATUSES = frozenset(
    {
        DepositStatus.paid.value,
        DepositStatus.failed.value,
        DepositStatus.expired.value,
        DepositStatus.cancelled.value,
    }
)
DEPOSIT_ALL_STATUSES = DEPOSIT_ACTIVE_STATUSES | DEPOSIT_TERMINAL_STATUSES
PAYMENT_METHOD_VALUES = frozenset(member.value for member in PaymentMethod)


def normalized_deposit_filters(
    query_params,
    *,
    valid_bank_codes: Iterable[str],
) -> dict[str, str]:
    view = str(query_params.get("view") or "active").strip().lower()
    if view not in {"active", "history"}:
        view = "active"
    bank_code = str(query_params.get("bank_code") or "").strip()
    if bank_code not in set(valid_bank_codes):
        bank_code = ""
    payment_method = str(query_params.get("payment_method") or "").strip()
    if payment_method not in PAYMENT_METHOD_VALUES:
        payment_method = ""
    status = str(query_params.get("status") or "").strip()
    allowed_statuses = DEPOSIT_ACTIVE_STATUSES if view == "active" else DEPOSIT_TERMINAL_STATUSES
    if status not in allowed_statuses:
        status = ""
    raw_date = str(query_params.get("deposit_date") or "").strip()
    try:
        date.fromisoformat(raw_date)
    except ValueError:
        raw_date = ""
    raw_amount = str(query_params.get("deposit_amount") or "").strip()
    if raw_amount:
        try:
            Decimal(raw_amount.replace(",", "."))
        except InvalidOperation:
            raw_amount = ""
    return {
        "view": view,
        "query": str(query_params.get("deposit_query") or "").strip(),
        "amount": raw_amount,
        "requisite": str(query_params.get("deposit_requisite") or "").strip(),
        "date": raw_date,
        "bank_code": bank_code,
        "payment_method": payment_method,
        "status": status,
    }


def trader_deposit_scope(requisite_ids: Iterable[object]):
    ids = list(requisite_ids)
    active_appeal_ids = select(Appeal.operation_id).where(
        Appeal.operation_type == "deposit",
        Appeal.status.in_([AppealStatus.opened.value, AppealStatus.in_review.value]),
    )
    return (
        select(Deposit)
        .where(Deposit.requisites_id.in_(ids), Deposit.id.not_in(active_appeal_ids))
        if ids
        else select(Deposit).where(False)
    )


def apply_trader_deposit_filters(
    statement,
    filters: dict[str, str],
    *,
    matching_requisite_ids: Iterable[object],
    requisite_filter_ids: Iterable[object],
    bank_requisite_ids: Iterable[object],
):
    view_statuses = (
        DEPOSIT_ACTIVE_STATUSES
        if filters.get("view") == "active"
        else DEPOSIT_TERMINAL_STATUSES
    )
    statement = statement.where(Deposit.status.in_(list(view_statuses)))
    query = filters.get("query", "")
    if query:
        pattern = f"%{query}%"
        query_clauses = [
            cast(Deposit.id, String).ilike(pattern),
            Deposit.external_id.ilike(pattern),
        ]
        requisite_ids = list(matching_requisite_ids)
        if requisite_ids:
            query_clauses.append(Deposit.requisites_id.in_(requisite_ids))
        statement = statement.where(or_(*query_clauses))
    amount = filters.get("amount", "")
    if amount:
        statement = statement.where(Deposit.amount == Decimal(amount.replace(",", ".")))
    raw_date = filters.get("date", "")
    if raw_date:
        selected_date = date.fromisoformat(raw_date)
        day_start = datetime.combine(selected_date, datetime.min.time(), tzinfo=timezone.utc)
        statement = statement.where(
            Deposit.created_at >= day_start,
            Deposit.created_at < day_start + timedelta(days=1),
        )
    if filters.get("requisite"):
        statement = statement.where(
            Deposit.requisites_id.in_(list(requisite_filter_ids))
        )
    bank_code = filters.get("bank_code", "")
    if bank_code:
        statement = statement.where(Deposit.requisites_id.in_(list(bank_requisite_ids)))
    payment_method = filters.get("payment_method", "")
    if payment_method:
        statement = statement.where(Deposit.method == payment_method)
    status = filters.get("status", "")
    if status:
        statement = statement.where(Deposit.status == status)
    if filters.get("view") == "active":
        statement = statement.order_by(
            Deposit.expires_at.is_(None), Deposit.expires_at.asc(), Deposit.created_at.asc()
        )
    else:
        statement = statement.order_by(Deposit.updated_at.desc(), Deposit.created_at.desc())
    return statement


async def trader_deposit_aggregates(
    db: AsyncSession,
    *,
    requisite_ids: Iterable[object],
    now: datetime | None = None,
) -> dict[str, object]:
    ids = list(requisite_ids)
    if not ids:
        return {
            "active_count": 0,
            "history_count": 0,
            "urgent_count": 0,
            "paid_today_count": 0,
            "paid_today_amount": Decimal("0.00"),
        }
    current = now or datetime.now(timezone.utc)
    day_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    next_day = day_start + timedelta(days=1)
    active_condition = Deposit.status.in_(list(DEPOSIT_ACTIVE_STATUSES))
    terminal_condition = Deposit.status.in_(list(DEPOSIT_TERMINAL_STATUSES))
    paid_today = (
        (Deposit.status == DepositStatus.paid.value)
        & (Deposit.updated_at >= day_start)
        & (Deposit.updated_at < next_day)
    )
    active_appeal_ids = select(Appeal.operation_id).where(
        Appeal.operation_type == "deposit",
        Appeal.status.in_([AppealStatus.opened.value, AppealStatus.in_review.value]),
    )
    statement = select(
        func.count(Deposit.id).filter(active_condition),
        func.count(Deposit.id).filter(terminal_condition),
        func.count(Deposit.id).filter(
            active_condition,
            Deposit.expires_at > current,
            Deposit.expires_at <= current + timedelta(minutes=5),
        ),
        func.count(Deposit.id).filter(paid_today),
        func.coalesce(func.sum(Deposit.amount).filter(paid_today), Decimal("0.00")),
    ).where(
        Deposit.requisites_id.in_(ids),
        Deposit.id.not_in(active_appeal_ids),
    )
    row = (await db.execute(statement)).one()
    return {
        "active_count": int(row[0] or 0),
        "history_count": int(row[1] or 0),
        "urgent_count": int(row[2] or 0),
        "paid_today_count": int(row[3] or 0),
        "paid_today_amount": Decimal(row[4] or 0),
    }
