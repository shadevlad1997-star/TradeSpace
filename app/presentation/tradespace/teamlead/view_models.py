"""Read-only TeamLead statements. No balance creation, reconciliation writes or commands."""
from __future__ import annotations
from app.presentation.tradespace.financial_copy import financial_description


from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import (
    Merchant, TeamLeadAccrual, TeamLeadBalance, TeamLeadLedgerEntry,
    TeamLeadMerchantAccrual, TeamLeadMerchantAssignment, TeamLeadSettlement,
    TeamLeadTraderAssignment, User,
)
from app.services.teamlead import TEAMLEAD_SETTLEMENT_COOLDOWN, TEAMLEAD_SETTLEMENT_FEE_USDT

ZERO = Decimal("0.00")
SOURCE_LABELS = {"trader_referral": "За трейдера", "merchant_referral": "За мерчанта"}
LEDGER_LABELS = {
    "accrual_credit": "Зачисление начисления", "debt_offset": "Погашение долга",
    "accrual_reversal": "Сторно начисления", "settlement_freeze": "Резервирование расчёта",
    "settlement_release": "Возврат резерва", "settlement_complete": "Завершение расчёта",
    "manual_adjustment": "Корректировка", "write_off": "Списание долга",
}
STATES = {
    "credited": ("Начислено", "positive"), "reversed": ("Сторнировано", "critical"),
    "pending": ("На рассмотрении", "attention"), "completed": ("Выполнен", "positive"),
    "rejected": ("Отклонён", "critical"),
}


def amount(value, precision=2):
    # Formatting stored Decimal values only; no commission or FX computation.
    return "—" if value is None else format(value, f".{precision}f")


def short(value):
    text = str(value)
    return text if len(text) <= 22 else text[:10] + "…" + text[-6:]


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


@dataclass(frozen=True)
class TeamLeadPage:
    balance: dict | None
    members: tuple[dict, ...]
    selected_member: dict | None
    member_requested: bool
    accruals: tuple[dict, ...]
    recent: tuple[dict, ...]
    ledger: tuple[dict, ...]
    settlements: tuple[dict, ...]
    source_summary: tuple[dict, ...]
    team_counts: dict
    filters: dict
    filter_error: str
    page: int
    pages: int
    filtered_count: int
    request_blockers: tuple[str, ...]
    next_settlement_at: datetime | None
    pending: bool
    settlement_key: str
    settlement_fee: str
    cooldown_hours: int
    attention: tuple[dict, ...]


async def load_teamlead_page(db: AsyncSession, user, *, cabinet_base: str, query_params, section: str):
    if user.role != "teamlead":
        raise ValueError("TeamLead presentation requires TeamLead actor")
    owner = user.id
    balance = await db.scalar(select(TeamLeadBalance).where(TeamLeadBalance.teamlead_id == owner))
    # Do not call get_teamlead_balance/reconcile_teamlead_account: they INSERT even on GET.
    balance_view = {key: amount(getattr(balance, key)) for key in (
        "available_rub", "frozen_rub", "debt_rub", "total_earned_rub", "total_paid_rub"
    )} if balance is not None else None
    assignments = list((await db.scalars(select(TeamLeadTraderAssignment).where(
        TeamLeadTraderAssignment.teamlead_id == owner
    ).order_by(TeamLeadTraderAssignment.effective_from.desc(), TeamLeadTraderAssignment.id))).all())
    merchant_assignments = list((await db.scalars(select(TeamLeadMerchantAssignment).where(
        TeamLeadMerchantAssignment.teamlead_id == owner
    ).order_by(TeamLeadMerchantAssignment.valid_from.desc(), TeamLeadMerchantAssignment.id))).all())
    trader_accruals = list((await db.scalars(select(TeamLeadAccrual).where(
        TeamLeadAccrual.teamlead_id == owner
    ).order_by(TeamLeadAccrual.created_at.desc(), TeamLeadAccrual.id).limit(500))).all())
    merchant_accruals = list((await db.scalars(select(TeamLeadMerchantAccrual).where(
        TeamLeadMerchantAccrual.teamlead_id == owner
    ).order_by(TeamLeadMerchantAccrual.created_at.desc(), TeamLeadMerchantAccrual.id).limit(500))).all())
    trader_ids = {r.trader_id for r in [*assignments, *trader_accruals]}
    merchant_ids = {r.merchant_id for r in [*merchant_assignments, *merchant_accruals]}
    trader_labels = {r.id: r for r in (await db.execute(select(User.id, User.email, User.is_active, User.is_locked)
        .where(User.id.in_(trader_ids)))).all()} if trader_ids else {}
    merchant_labels = dict((await db.execute(select(Merchant.id, Merchant.name)
        .where(Merchant.id.in_(merchant_ids)))).all()) if merchant_ids else {}
    members = {}
    accrual_views = []
    source_summary = []
    for source, rows in (("trader_referral", trader_accruals), ("merchant_referral", merchant_accruals)):
        source_summary.append({
            "source": source, "label": SOURCE_LABELS[source], "records": len(rows),
            "credited": amount(sum((r.accrual_rub for r in rows if r.status == "credited"), ZERO)),
            "reversed": amount(sum((r.accrual_rub for r in rows if r.status == "reversed"), ZERO)),
        })
        for row in rows:
            participant_id = row.trader_id if source == "trader_referral" else row.merchant_id
            key = ("trader:" if source == "trader_referral" else "merchant:") + str(participant_id)
            label = (trader_labels[participant_id].email if participant_id in trader_labels else "Архивный трейдер") if source == "trader_referral" else merchant_labels.get(participant_id, "Архивный мерчант")
            status_label, tone = STATES.get(row.status, ("Статус не определён", "neutral"))
            accrual_views.append({
                "id": str(row.id), "short_id": short(row.id), "member_key": key,
                "label": label, "source": source, "source_label": SOURCE_LABELS[source],
                "deposit_id": str(row.deposit_id), "deposit_short": short(row.deposit_id),
                "gross": amount(row.gross_rub), "percent": amount(row.commission_percent_snapshot, 6),
                "accrual": amount(row.accrual_rub), "available": amount(row.credited_to_available_rub),
                "debt": amount(row.applied_to_debt_rub), "status": row.status,
                "status_label": status_label, "tone": tone, "created_at": row.created_at,
                "reversed_at": row.reversed_at, "reason": row.reversal_reason,
            })
    for kind, rows in (("trader", assignments), ("merchant", merchant_assignments)):
        for row in rows:
            entity_id = row.trader_id if kind == "trader" else row.merchant_id
            key = kind + ":" + str(entity_id)
            start = row.effective_from if kind == "trader" else row.valid_from
            end = row.effective_to if kind == "trader" else row.valid_to
            if key not in members:
                account = trader_labels.get(entity_id) if kind == "trader" else None
                members[key] = {
                    "key": key, "kind": kind, "id": str(entity_id),
                    "label": (account.email if account else "Архивный трейдер") if kind == "trader" else merchant_labels.get(entity_id, "Архивный мерчант"),
                    "kind_label": "Трейдер" if kind == "trader" else "Мерчант",
                    "account_status": ("Заблокирован" if account.is_locked else "Активен" if account.is_active else "Неактивен") if account else None,
                    "active": False, "history": [],
                    "url": cabinet_base + "/tradespace/team?member=" + key,
                }
            member = members[key]
            member["active"] |= end is None
            member["history"].append({"from": start, "to": end, "percent": amount(row.commission_percent, 6)})
    for member in members.values():
        member["records"] = [r for r in accrual_views if r["member_key"] == member["key"]]
        member["record_count"] = len(member["records"])
    requested = str(query_params.get("member") or "")
    selected = members.get(requested) if requested else None
    filters = {key: str(query_params.get(key) or "").strip() for key in ("q", "kind", "status", "source", "date_from", "date_to")}
    error = ""
    bounds = {}
    for key in ("date_from", "date_to"):
        if filters[key]:
            try:
                bounds[key] = datetime.strptime(filters[key], "%Y-%m-%d").date()
            except ValueError:
                error = "Проверьте даты: используйте формат ГГГГ-ММ-ДД."
    if len(bounds) == 2 and bounds["date_from"] > bounds["date_to"]:
        error = "Начало периода должно быть не позже окончания."
    ordered = sorted(accrual_views, key=lambda r: (r["created_at"], r["id"]), reverse=True)
    filtered = []
    for row in ordered:
        if filters["q"] and filters["q"].casefold() not in (row["label"] + row["deposit_id"] + row["id"]).casefold():
            continue
        if filters["source"] and row["source"] != filters["source"]:
            continue
        if filters["status"] and section == "accruals" and row["status"] != filters["status"]:
            continue
        day = row["created_at"].date()
        if bounds.get("date_from") and day < bounds["date_from"] or bounds.get("date_to") and day > bounds["date_to"]:
            continue
        filtered.append(row)
    if error:
        filtered = []
    team_rows = sorted(members.values(), key=lambda r: (not r["active"], r["label"].casefold(), r["key"]))
    team_counts = {kind: sum(r["active"] for r in team_rows if r["kind"] == kind) for kind in ("trader", "merchant")}
    team_rows = [r for r in team_rows if
        (not filters["q"] or filters["q"].casefold() in (r["label"] + r["id"]).casefold()) and
        (not filters["kind"] or r["kind"] == filters["kind"]) and
        (section != "team" or not filters["status"] or ("active" if r["active"] else "closed") == filters["status"])]
    try:
        page = max(1, int(query_params.get("page", "1")))
    except (ValueError, TypeError):
        page = 1
    pages = max(1, (len(filtered) + 24) // 25)
    page = min(page, pages)
    settlements = list((await db.scalars(select(TeamLeadSettlement).where(
        TeamLeadSettlement.teamlead_id == owner
    ).order_by(TeamLeadSettlement.requested_at.desc(), TeamLeadSettlement.id).limit(200))).all())
    settlement_views = tuple({
        "id": str(r.id), "short_id": short(r.id), "status": r.status,
        "status_label": STATES.get(r.status, ("Статус не определён", "neutral"))[0],
        "tone": STATES.get(r.status, ("Статус не определён", "neutral"))[1],
        "requested_usdt": amount(r.requested_usdt, 6), "fee_usdt": amount(r.fee_usdt, 6),
        "requested_rub": amount(r.requested_rub), "fee_rub": amount(r.fee_rub),
        "total_debit_rub": amount(r.total_debit_rub), "rate": amount(r.rapira_rate_rub, 8),
        "network": r.network, "wallet": r.wallet_address, "tx_hash": r.tx_hash,
        "reject_reason": r.reject_reason, "requested_at": r.requested_at,
        "completed_at": r.completed_at, "rejected_at": r.rejected_at,
        "fetched_at": r.fetched_at, "provider_timestamp": r.provider_timestamp,
        "rate_source": r.rate_source, "freshness_basis": r.freshness_basis,
        "rate_symbol": r.rate_symbol, "rate_side": r.rate_side,
    } for r in settlements)
    pending = bool(await db.scalar(select(TeamLeadSettlement.id).where(
        TeamLeadSettlement.teamlead_id == owner, TeamLeadSettlement.status == "pending").limit(1)))
    last_completed = await db.scalar(select(func.max(TeamLeadSettlement.completed_at)).where(
        TeamLeadSettlement.teamlead_id == owner, TeamLeadSettlement.status == "completed"))
    next_at = utc(last_completed) + TEAMLEAD_SETTLEMENT_COOLDOWN if last_completed else None
    blockers = []
    if balance is None:
        blockers.append("Финансовый счёт ещё не сформирован. Запрос станет доступен после появления средств.")
    elif balance.debt_rub > 0:
        blockers.append("Есть непогашенный долг. Новые начисления сначала направляются на его погашение.")
    elif balance.available_rub <= 0:
        blockers.append("Для запроса нет доступных средств.")
    if pending:
        blockers.append("Предыдущий запрос на рассмотрении. Дождитесь выполнения или отклонения.")
    if next_at and next_at > datetime.now(timezone.utc):
        blockers.append("После выполненного расчёта действует пауза 168 часов.")
    ledger_rows = list((await db.scalars(select(TeamLeadLedgerEntry).where(
        TeamLeadLedgerEntry.teamlead_id == owner
    ).order_by(TeamLeadLedgerEntry.created_at.desc(), TeamLeadLedgerEntry.id).limit(300))).all())
    ledger = tuple({
        "id": str(r.id), "created_at": r.created_at, "type": r.entry_type,
        "label": LEDGER_LABELS.get(r.entry_type, "Финансовое движение"),
        "source": SOURCE_LABELS.get(r.source_type, "Финансовый счёт TeamLead"),
        "amount": amount(r.amount_rub), "available": amount(r.available_after),
        "frozen": amount(r.frozen_after), "debt": amount(r.debt_after), "reason": financial_description(r.reason, user_comment=r.entry_type in {"manual_adjustment","write_off","settlement_release","accrual_reversal"}),
    } for r in ledger_rows)
    attention = [{"title": "Расчёты требуют внимания", "text": text, "url": cabinet_base + "/tradespace/settlements"} for text in blockers if balance is not None]
    return TeamLeadPage(
        balance_view, tuple(team_rows), selected, bool(requested),
        tuple(filtered[(page-1)*25:page*25]), tuple(ordered[:6]), ledger, settlement_views,
        tuple(source_summary), team_counts, filters, error, page, pages, len(filtered),
        tuple(blockers), next_at, pending, f"teamlead-settlement:{owner}:{uuid4().hex}",
        amount(TEAMLEAD_SETTLEMENT_FEE_USDT, 6),
        int(TEAMLEAD_SETTLEMENT_COOLDOWN.total_seconds() // 3600), tuple(attention),
    )
