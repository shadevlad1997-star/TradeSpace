from __future__ import annotations
from app.presentation.tradespace.financial_copy import financial_description


from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import AppealStatus, DepositStatus
from app.presentation.tradespace.labels import reason_label
from app.core.mobile_operators import get_enabled_mobile_operators
from app.core.payment_methods import PAYMENT_METHOD_OPTIONS, payment_method_label
from app.core.requisite_providers import requisite_provider_display, requisite_provider_label
from app.core.russian_banks import get_enabled_banks
from app.models import Appeal, AppealMessage, Deposit, Requisite, SmsMessage, TraderLedgerEntry, User
from app.services.appeals import (
    TRADER_REJECTION_REASONS,
    appeal_deadline,
    appeal_remaining_seconds,
    metadata as appeal_metadata,
)
from app.services.deposit_ttl import deposit_deadline
from app.services.finance_reconciliation import reconcile_trader_balance
from app.services.requisites import (
    deposit_trader_hold_amount,
    deposit_trader_profit_amount,
    deposit_trader_settlement_amount,
)
TRADER_ROLES = frozenset({"operator", "trader"})
DEPOSIT_ACTIVE_STATUSES = frozenset({
    DepositStatus.created.value,
    DepositStatus.pending.value,
    DepositStatus.appeal_opened.value,
})
DEPOSIT_TERMINAL_STATUSES = frozenset({
    DepositStatus.paid.value,
    DepositStatus.failed.value,
    DepositStatus.expired.value,
    DepositStatus.cancelled.value,
})


def short_identifier(value: object, *, head: int = 8, tail: int = 6) -> str:
    text = str(value or "")
    if len(text) <= head + tail + 1:
        return text or "—"
    return f"{text[:head]}…{text[-tail:]}"
_ACTIVE_APPEAL_STATUSES = frozenset({AppealStatus.opened.value, AppealStatus.in_review.value})
_STATUS_LABELS = {
    DepositStatus.created.value: ("Создана", "neutral"),
    DepositStatus.pending.value: ("Ожидает оплаты", "attention"),
    DepositStatus.paid.value: ("Оплачена", "positive"),
    DepositStatus.failed.value: ("Не оплачена", "critical"),
    DepositStatus.expired.value: ("Срок истёк", "critical"),
    DepositStatus.cancelled.value: ("Отменена", "neutral"),
    DepositStatus.appeal_opened.value: ("Открыт спор", "attention"),
}
_APPEAL_LABELS = {
    AppealStatus.opened.value: ("Нужен ответ", "attention"),
    AppealStatus.in_review.value: ("На проверке", "attention"),
    AppealStatus.approved.value: ("Одобрен", "positive"),
    AppealStatus.rejected.value: ("Отклонён", "critical"),
    AppealStatus.returned_to_processing.value: ("Возвращён в работу", "neutral"),
    AppealStatus.closed.value: ("Закрыт", "neutral"),
}
_REQUISITE_LABELS = {
    "active": ("Активен", "positive"),
    "disabled": ("Отключён", "neutral"),
    "review": ("На проверке", "attention"),
    "deleted": ("Удалён", "critical"),
}
_LEDGER_LABELS = {
    "hold": "Резерв",
    "release_hold": "Освобождение резерва",
    "deposit_success_debit": "Списание по оплате",
    "executor_fee": "Комиссия трейдера",
    "manual_adjustment": "Ручная корректировка",
}


def _money(value: object) -> str:
    return f"{Decimal(str(value or '0')).quantize(Decimal('0.01')):.2f}"


def _status(value: str, reason: str = "") -> tuple[str, str]:
    if value == DepositStatus.failed.value and reason == "trader_timeout":
        return "Время истекло", "critical"
    return _STATUS_LABELS.get(value, ("Статус не определён", "neutral"))


def _requisite_search_text(row: Requisite) -> str:
    from app.web.routes import display_requisite_value

    return " ".join(
        (
            display_requisite_value(row),
            row.bank_code or "",
            row.bank_name or "",
            row.operator_code or "",
            row.full_name or "",
            row.owner_name or "",
        )
    ).lower()


@dataclass(frozen=True, slots=True)
class TraderSmsView:
    created_at: datetime
    sender: str
    parsed_amount: str
    review_required: bool


@dataclass(frozen=True, slots=True)
class TraderTimelineEvent:
    created_at: datetime
    label: str
    tone: str = "neutral"
    description: str = ""


@dataclass(frozen=True, slots=True)
class TraderOperationView:
    id: str
    short_id: str
    external_id: str
    external_id_short: str
    amount: str
    currency: str
    method: str
    method_label: str
    status: str
    status_label: str
    status_tone: str
    reason: str
    requisite: str
    provider_label: str
    provider_name: str
    recipient_name: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    remaining_seconds: int
    is_urgent: bool
    can_confirm: bool
    trader_hold: str
    trader_settlement: str
    trader_profit: str
    hold_status: str
    detail_url: str
    confirm_url: str
    @property
    def current_hold(self):
        if self.hold_status in {'released', 'settled'}:
            return '0.00'
        if self.hold_status == 'active' and self.status in DEPOSIT_ACTIVE_STATUSES | {'appeal_opened'}:
            return self.trader_hold
        return None

    @property
    def reserve_label(self):
        return {'released': 'Резерв освобождён', 'settled': 'Резерв списан'}.get(self.hold_status, 'Текущий резерв')

    has_appeal: bool = False
    appeal_status: str = ""
    sms_evidence: tuple[TraderSmsView, ...] = ()
    timeline: tuple[TraderTimelineEvent, ...] = ()


@dataclass(frozen=True, slots=True)
class TraderRequisiteView:
    id: str
    value: str
    method: str
    method_label: str
    provider_name: str
    owner_name: str
    status: str
    status_label: str
    status_tone: str
    traffic_status: str
    traffic_label: str
    enabled: bool
    is_available: bool
    daily_limit: str
    operation_limit: int
    simultaneous_limit: int
    min_check: str
    max_check: str
    usage_count: int
    last_success_at: datetime | None
    bank_code: str
    operator_code: str
    automation_id: str
    last4: str
    request_count: int
    timeframe: str
    success_delay_minutes: int
    toggle_url: str
    edit_url: str
    delete_url: str


@dataclass(frozen=True, slots=True)
class TraderLedgerView:
    created_at: datetime
    entry_type: str
    entry_label: str
    amount: str
    balance_after: str
    hold_after: str
    operation_id: str
    operation_short: str
    description: str
    reference_type: str = ""
    reference_label: str = ""


@dataclass(frozen=True, slots=True)
class TraderAppealMessageView:
    created_at: datetime
    author_label: str
    message: str
    has_attachment: bool


@dataclass(frozen=True, slots=True)
class TraderAppealView:
    id: str
    short_id: str
    operation_id: str
    operation_short: str
    operation_url: str
    status: str
    status_label: str
    status_tone: str
    decision: str
    amount: str
    currency: str
    requisite: str
    provider_name: str
    created_at: datetime
    deadline_at: datetime
    remaining_seconds: int
    can_act: bool
    receipt_url: str
    receipt_name: str
    statement_url: str
    statement_name: str
    rejection_reason: str
    corrected_amount: str
    messages: tuple[TraderAppealMessageView, ...]
    accept_url: str
    reject_url: str


@dataclass(frozen=True, slots=True)
class TraderAnalytics:
    terminal_count: int
    paid_count: int
    failed_count: int
    paid_volume: str
    paid_profit: str
    paid_share: str
    method_rows: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class TraderPageData:
    balance: str
    hold: str
    available: str
    finance_reconciled: bool
    traffic_status: str
    traffic_label: str
    active_operations: tuple[TraderOperationView, ...] = ()
    recent_operations: tuple[TraderOperationView, ...] = ()
    requisites: tuple[TraderRequisiteView, ...] = ()
    ledger: tuple[TraderLedgerView, ...] = ()
    appeals: tuple[TraderAppealView, ...] = ()
    aggregates: dict[str, Any] = field(default_factory=dict)
    filters: dict[str, str] = field(default_factory=dict)
    analytics: TraderAnalytics | None = None
    selected_operation: TraderOperationView | None = None
    active_appeal_count: int = 0
    unavailable_requisite_count: int = 0
    banks: tuple[dict[str, str], ...] = ()
    operators: tuple[dict[str, str], ...] = ()
    payment_methods: tuple[dict[str, str], ...] = PAYMENT_METHOD_OPTIONS
    rejection_reasons: dict[str, str] = field(default_factory=dict)


def _traffic_label(value: str) -> str:
    return {
        "active": "Трафик активен",
        "under_review": "Под наблюдением",
        "reinstated_limited": "Трафик ограничен",
        "reinstated_full": "Трафик восстановлен",
        "auto_paused": "Трафик приостановлен",
        "manual_paused": "Трафик приостановлен",
        "blocked": "Трафик заблокирован",
    }.get(value, "Статус трафика не определён")


def _operation_view(
    deposit: Deposit,
    requisite: Requisite | None,
    trader: User,
    *,
    cabinet_base: str,
    active_appeal: Appeal | None = None,
    sms_rows: tuple[TraderSmsView, ...] = (),
) -> TraderOperationView:
    from app.web.routes import display_requisite_value

    now = datetime.now(timezone.utc)
    deadline = deposit_deadline(deposit)
    remaining = max(0, int((deadline - now).total_seconds()))
    meta = deposit.metadata_json if isinstance(deposit.metadata_json, dict) else {}
    reason = str(meta.get("failure_reason") or "")
    status_label, status_tone = _status(deposit.status, reason)
    reason = reason_label(reason)
    provider_name = (
        requisite_provider_display(
            requisite.method,
            requisite.bank_code,
            requisite.operator_code,
            requisite.bank_name,
        )
        if requisite
        else ""
    )
    timeline: list[TraderTimelineEvent] = [
        TraderTimelineEvent(deposit.created_at, "Операция назначена", "neutral"),
    ]
    if sms_rows:
        timeline.append(TraderTimelineEvent(sms_rows[-1].created_at, "Получено платёжное подтверждение", "attention"))
    if active_appeal:
        timeline.append(TraderTimelineEvent(active_appeal.created_at, "Открыт спор", "attention"))
    if deposit.status == DepositStatus.paid.value:
        timeline.append(TraderTimelineEvent(deposit.updated_at, "Оплата подтверждена", "positive"))
    elif deposit.status in DEPOSIT_TERMINAL_STATUSES:
        timeline.append(TraderTimelineEvent(deposit.updated_at, status_label, status_tone, reason))
    return TraderOperationView(
        id=str(deposit.id),
        short_id=short_identifier(deposit.id),
        external_id=str(deposit.external_id or ""),
        external_id_short=short_identifier(deposit.external_id),
        amount=_money(deposit.amount),
        currency=str(deposit.currency or "RUB"),
        method=str(deposit.method or ""),
        method_label=payment_method_label(deposit.method),
        status=str(deposit.status or ""),
        status_label=status_label,
        status_tone=status_tone,
        reason=reason,
        requisite=display_requisite_value(requisite) if requisite else "",
        provider_label=requisite_provider_label(requisite.method) if requisite else "Провайдер",
        provider_name=provider_name,
        recipient_name=(requisite.full_name or requisite.owner_name or "") if requisite else "",
        created_at=deposit.created_at,
        updated_at=deposit.updated_at,
        expires_at=deadline,
        remaining_seconds=remaining,
        is_urgent=deposit.status in DEPOSIT_ACTIVE_STATUSES and 0 < remaining <= 300,
        can_confirm=bool(
            requisite
            and str(requisite.trader_id) == str(trader.id)
            and deposit.status in DEPOSIT_ACTIVE_STATUSES
            and active_appeal is None
        ),
        trader_hold=_money(deposit_trader_hold_amount(deposit, trader)),
        trader_settlement=_money(deposit_trader_settlement_amount(deposit, trader)),
        trader_profit=_money(deposit_trader_profit_amount(deposit, trader)),
        hold_status=str(meta.get("trader_hold_status") or ""),
        detail_url=f"{cabinet_base}/tradespace/operations/{deposit.id}",
        confirm_url=f"{cabinet_base}/deposits/{deposit.id}/confirm",
        has_appeal=active_appeal is not None,
        appeal_status=str(active_appeal.status if active_appeal else ""),
        sms_evidence=sms_rows,
        timeline=tuple(sorted(timeline, key=lambda item: item.created_at)),
    )


def _requisite_view(row: Requisite, *, cabinet_base: str) -> TraderRequisiteView:
    from app.web.routes import display_requisite_value

    label, tone = _REQUISITE_LABELS.get(str(row.status), ("Статус не определён", "neutral"))
    available = bool(row.enabled and row.status == "active" and row.traffic_status in {"active", "reinstated_full"})
    return TraderRequisiteView(
        id=str(row.id),
        value=display_requisite_value(row),
        method=str(row.method),
        method_label=payment_method_label(row.method),
        provider_name=requisite_provider_display(row.method, row.bank_code, row.operator_code, row.bank_name),
        owner_name=str(row.full_name or row.owner_name or ""),
        status=str(row.status),
        status_label=label,
        status_tone=tone if available or row.status != "active" else "attention",
        traffic_status=str(row.traffic_status or ""),
        traffic_label=_traffic_label(str(row.traffic_status or "")),
        enabled=bool(row.enabled),
        is_available=available,
        daily_limit=_money(row.daily_limit),
        operation_limit=int(row.operation_limit or 0),
        simultaneous_limit=int(row.simultaneous_limit or 0),
        min_check=_money(row.min_check),
        max_check=_money(row.max_check),
        usage_count=int(row.usage_count or 0),
        last_success_at=row.last_success_at,
        bank_code=str(row.bank_code or ""),
        operator_code=str(row.operator_code or ""),
        automation_id=str(row.automation_id or ""),
        last4=str(row.last4 or ""),
        request_count=int(row.request_count or 0),
        timeframe=str(row.timeframe or "час"),
        success_delay_minutes=int(row.success_delay_minutes or 0),
        toggle_url=f"{cabinet_base}/requisites/{row.id}/toggle",
        edit_url=f"{cabinet_base}/requisites/{row.id}/edit",
        delete_url=f"{cabinet_base}/requisites/{row.id}/delete",
    )


async def _own_requisites(db: AsyncSession, trader_id: UUID) -> list[Requisite]:
    return list(
        (
            await db.execute(
                select(Requisite)
                .where(Requisite.trader_id == trader_id, Requisite.is_archived.is_(False))
                .order_by(Requisite.enabled.desc(), Requisite.created_at.desc())
            )
        )
        .scalars()
        .all()
    )


async def _active_appeals(db: AsyncSession, *, deposit_ids: list[UUID]) -> dict[UUID, Appeal]:
    if not deposit_ids:
        return {}
    rows = (
        await db.execute(
            select(Appeal)
            .where(
                Appeal.operation_type == "deposit",
                Appeal.operation_id.in_(deposit_ids),
                Appeal.status.in_(list(_ACTIVE_APPEAL_STATUSES)),
            )
            .order_by(Appeal.created_at.desc())
        )
    ).scalars().all()
    result: dict[UUID, Appeal] = {}
    for row in rows:
        result.setdefault(row.operation_id, row)
    return result


async def _sms_by_deposit(db: AsyncSession, deposit_ids: list[UUID]) -> dict[UUID, tuple[TraderSmsView, ...]]:
    if not deposit_ids:
        return {}
    rows = (
        await db.execute(
            select(SmsMessage)
            .where(SmsMessage.linked_deposit_id.in_(deposit_ids))
            .order_by(SmsMessage.created_at.asc())
        )
    ).scalars().all()
    result: dict[UUID, list[TraderSmsView]] = {}
    for row in rows:
        result.setdefault(row.linked_deposit_id, []).append(
            TraderSmsView(
                created_at=row.created_at,
                sender=str(row.sender or "Источник"),
                parsed_amount=_money(row.parsed_amount) if row.parsed_amount is not None else "",
                review_required=bool(row.review_required),
            )
        )
    return {key: tuple(value) for key, value in result.items()}


async def load_operations(
    db: AsyncSession,
    trader: User,
    *,
    cabinet_base: str,
    query_params,
    view: str,
    limit: int = 200,
) -> tuple[tuple[TraderOperationView, ...], list[Requisite], dict[str, str], dict[str, Any]]:
    from app.web.ui_queries import (
        apply_trader_deposit_filters,
        normalized_deposit_filters,
        trader_deposit_aggregates,
        trader_deposit_scope,
    )

    requisites = await _own_requisites(db, trader.id)
    normalized_params = dict(query_params)
    normalized_params["view"] = view
    filters = normalized_deposit_filters(
        normalized_params,
        valid_bank_codes=(bank.code for bank in get_enabled_banks()),
    )
    filters["view"] = view
    requisite_ids = [row.id for row in requisites]
    query_text = filters.get("query", "").lower()
    requisite_text = filters.get("requisite", "").lower()
    matching_ids = [row.id for row in requisites if query_text and query_text in _requisite_search_text(row)]
    requisite_filter_ids = [row.id for row in requisites if requisite_text and requisite_text in _requisite_search_text(row)]
    bank_ids = [row.id for row in requisites if filters.get("bank_code") and row.bank_code == filters["bank_code"]]
    statement = apply_trader_deposit_filters(
        trader_deposit_scope(requisite_ids),
        filters,
        matching_requisite_ids=matching_ids,
        requisite_filter_ids=requisite_filter_ids,
        bank_requisite_ids=bank_ids,
    ).limit(max(1, min(limit, 500)))
    deposits = list((await db.execute(statement)).scalars().all()) if requisite_ids else []
    req_by_id = {row.id: row for row in requisites}
    appeal_map = await _active_appeals(db, deposit_ids=[row.id for row in deposits])
    sms_map = await _sms_by_deposit(db, [row.id for row in deposits])
    rows = tuple(
        _operation_view(
            row,
            req_by_id.get(row.requisites_id),
            trader,
            cabinet_base=cabinet_base,
            active_appeal=appeal_map.get(row.id),
            sms_rows=sms_map.get(row.id, ()),
        )
        for row in deposits
    )
    aggregates = await trader_deposit_aggregates(db, requisite_ids=requisite_ids)
    return rows, requisites, filters, aggregates


async def load_operation_detail(
    db: AsyncSession,
    trader: User,
    *,
    cabinet_base: str,
    operation_id: UUID,
) -> TraderOperationView | None:
    requisites = await _own_requisites(db, trader.id)
    req_by_id = {row.id: row for row in requisites}
    if not req_by_id:
        return None
    deposit = (
        await db.execute(
            select(Deposit).where(
                Deposit.id == operation_id,
                Deposit.requisites_id.in_(list(req_by_id)),
            )
        )
    ).scalar_one_or_none()
    if not deposit:
        return None
    appeal_map = await _active_appeals(db, deposit_ids=[deposit.id])
    sms_map = await _sms_by_deposit(db, [deposit.id])
    return _operation_view(
        deposit,
        req_by_id.get(deposit.requisites_id),
        trader,
        cabinet_base=cabinet_base,
        active_appeal=appeal_map.get(deposit.id),
        sms_rows=sms_map.get(deposit.id, ()),
    )


async def load_appeals(db: AsyncSession, trader: User, *, cabinet_base: str) -> tuple[TraderAppealView, ...]:
    from app.web.routes import display_requisite_value

    requisites = await _own_requisites(db, trader.id)
    req_by_id = {row.id: row for row in requisites}
    if not req_by_id:
        return ()
    deposits = list(
        (
            await db.execute(select(Deposit).where(Deposit.requisites_id.in_(list(req_by_id))))
        )
        .scalars()
        .all()
    )
    dep_by_id = {row.id: row for row in deposits}
    if not dep_by_id:
        return ()
    appeals = list(
        (
            await db.execute(
                select(Appeal)
                .where(
                    Appeal.operation_type == "deposit",
                    Appeal.operation_id.in_(list(dep_by_id)),
                )
                .order_by(Appeal.created_at.desc())
                .limit(500)
            )
        )
        .scalars()
        .all()
    )
    if not appeals:
        return ()
    messages = list(
        (
            await db.execute(
                select(AppealMessage)
                .where(AppealMessage.appeal_id.in_([row.id for row in appeals]))
                .order_by(AppealMessage.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    messages_by_id: dict[UUID, list[TraderAppealMessageView]] = {}
    for message in messages:
        author = "Вы" if str(message.author_id or "") == str(trader.id) else "Мерчант / система"
        messages_by_id.setdefault(message.appeal_id, []).append(
            TraderAppealMessageView(
                created_at=message.created_at,
                author_label=author,
                message=str(message.message or ""),
                has_attachment=bool(message.attachment_path),
            )
        )
    result: list[TraderAppealView] = []
    for appeal in appeals:
        deposit = dep_by_id[appeal.operation_id]
        requisite = req_by_id.get(deposit.requisites_id)
        meta = appeal_metadata(appeal)
        status_label, tone = _APPEAL_LABELS.get(str(appeal.status), ("Статус не определён", "neutral"))
        receipt = meta.get("receipt_file") if isinstance(meta.get("receipt_file"), dict) else {}
        statement = meta.get("statement_file") if isinstance(meta.get("statement_file"), dict) else {}
        receipt_path = str(receipt.get("path") or "")
        statement_path = str(statement.get("path") or "")
        result.append(
            TraderAppealView(
                id=str(appeal.id),
                short_id=short_identifier(appeal.id),
                operation_id=str(deposit.id),
                operation_short=short_identifier(deposit.id),
                operation_url=f"{cabinet_base}/tradespace/operations/{deposit.id}",
                status=str(appeal.status),
                status_label=status_label,
                status_tone=tone,
                decision=str(appeal.decision or ""),
                amount=_money(meta.get("amount_claimed") or deposit.amount),
                currency=str(deposit.currency or "RUB"),
                requisite=str(meta.get("requisite") or (display_requisite_value(requisite) if requisite else "")),
                provider_name=str(
                    meta.get("recipient_bank")
                    or (
                        requisite_provider_display(
                            requisite.method,
                            requisite.bank_code,
                            requisite.operator_code,
                            requisite.bank_name,
                        )
                        if requisite
                        else ""
                    )
                ),
                created_at=appeal.created_at,
                deadline_at=appeal_deadline(appeal),
                remaining_seconds=appeal_remaining_seconds(appeal),
                can_act=bool(
                    appeal.status == AppealStatus.opened.value
                    and str(meta.get("trader_id") or (requisite.trader_id if requisite else "")) == str(trader.id)
                ),
                receipt_url=f"{cabinet_base}/appeals/files/{receipt_path}" if receipt_path else "",
                receipt_name=str(receipt.get("original_name") or "Чек"),
                statement_url=f"{cabinet_base}/appeals/files/{statement_path}" if statement_path else "",
                statement_name=str(statement.get("original_name") or "Выписка"),
                rejection_reason=str(meta.get("trader_rejection_reason_label") or ""),
                corrected_amount=str(meta.get("corrected_amount") or ""),
                messages=tuple(messages_by_id.get(appeal.id, ())),
                accept_url=f"{cabinet_base}/appeals/{appeal.id}/trader/accept",
                reject_url=f"{cabinet_base}/appeals/{appeal.id}/trader/reject",
            )
        )
    return tuple(result)


async def load_ledger(db: AsyncSession, trader_id: UUID, *, limit: int = 200) -> tuple[TraderLedgerView, ...]:
    rows = (
        await db.execute(
            select(TraderLedgerEntry)
            .where(TraderLedgerEntry.trader_id == trader_id)
            .order_by(TraderLedgerEntry.created_at.desc(), TraderLedgerEntry.id.desc())
            .limit(max(1, min(limit, 500)))
        )
    ).scalars().all()
    reference_ids = {row.operation_id for row in rows if row.operation_id}
    owned_deposits = set((await db.scalars(select(Deposit.id).join(Requisite, Requisite.id == Deposit.requisites_id)
                         .where(Deposit.id.in_(reference_ids), Requisite.trader_id == trader_id))).all())
    owned_requisites = set((await db.scalars(select(Requisite.id).where(Requisite.id.in_(reference_ids),
                           Requisite.trader_id == trader_id))).all())
    return tuple(
        TraderLedgerView(
            created_at=row.created_at,
            entry_type=str(row.entry_type or ""),
            entry_label=_LEDGER_LABELS.get(str(row.entry_type or ""), "Финансовое движение"),
            amount=_money(row.amount),
            balance_after=_money(row.balance_after),
            hold_after=_money(row.hold_after),
            operation_id=str(row.operation_id) if row.operation_id in owned_deposits else "",
            operation_short=short_identifier(row.operation_id) if row.operation_id else "—",
            description=financial_description(row.description, user_comment=row.entry_type in {'balance_adjustment','insurance_deposit_set'}),
            reference_type='deposit' if row.operation_id in owned_deposits else ('requisite' if row.operation_id in owned_requisites else ''),
            reference_label='Операция' if row.operation_id in owned_deposits else ('Реквизит' if row.operation_id in owned_requisites else ''),
        )
        for row in rows
    )


async def load_analytics(db: AsyncSession, trader: User) -> TraderAnalytics:
    requisites = await _own_requisites(db, trader.id)
    if not requisites:
        return TraderAnalytics(0, 0, 0, "0.00", "0.00", "—", ())
    deposits = list(
        (
            await db.execute(
                select(Deposit)
                .where(Deposit.requisites_id.in_([row.id for row in requisites]))
                .order_by(Deposit.created_at.desc())
                .limit(2000)
            )
        )
        .scalars()
        .all()
    )
    terminal = [row for row in deposits if row.status in DEPOSIT_TERMINAL_STATUSES]
    paid = [row for row in terminal if row.status == DepositStatus.paid.value]
    failed = [row for row in terminal if row.status != DepositStatus.paid.value]
    method_map: dict[str, dict[str, Any]] = {}
    for row in terminal:
        slot = method_map.setdefault(
            str(row.method),
            {"method": str(row.method), "label": payment_method_label(row.method), "closed": 0, "paid": 0, "paid_volume": Decimal("0.00")},
        )
        slot["closed"] += 1
        if row.status == DepositStatus.paid.value:
            slot["paid"] += 1
            slot["paid_volume"] += Decimal(row.amount)
    method_rows = tuple(
        {
            **row,
            "paid_volume": _money(row["paid_volume"]),
            "paid_share": f"{(Decimal(row['paid']) / Decimal(row['closed']) * Decimal('100')).quantize(Decimal('0.1'))}%"
            if row["closed"]
            else "—",
        }
        for row in sorted(method_map.values(), key=lambda item: item["paid_volume"], reverse=True)
    )
    paid_share = (
        f"{(Decimal(len(paid)) / Decimal(len(terminal)) * Decimal('100')).quantize(Decimal('0.1'))}%"
        if terminal
        else "—"
    )
    return TraderAnalytics(
        terminal_count=len(terminal),
        paid_count=len(paid),
        failed_count=len(failed),
        paid_volume=_money(sum((Decimal(row.amount) for row in paid), Decimal("0.00"))),
        paid_profit=_money(sum((deposit_trader_profit_amount(row, trader) for row in paid), Decimal("0.00"))),
        paid_share=paid_share,
        method_rows=method_rows,
    )


async def load_trader_page(
    db: AsyncSession,
    trader: User,
    *,
    cabinet_base: str,
    query_params,
    section: str,
    selected_operation_id: UUID | None = None,
) -> TraderPageData:
    if trader.role not in TRADER_ROLES:
        raise PermissionError("trader role required")
    reconciliation = await reconcile_trader_balance(db, trader.id)
    active_operations: tuple[TraderOperationView, ...] = ()
    recent_operations: tuple[TraderOperationView, ...] = ()
    requisites: list[Requisite] = []
    filters: dict[str, str] = {}
    aggregates: dict[str, Any] = {}
    ledger: tuple[TraderLedgerView, ...] = ()
    appeals: tuple[TraderAppealView, ...] = ()
    analytics: TraderAnalytics | None = None
    selected: TraderOperationView | None = None

    if section in {"work", "notifications"}:
        active_operations, requisites, filters, aggregates = await load_operations(
            db, trader, cabinet_base=cabinet_base, query_params=query_params, view="active", limit=200
        )
        history_params = dict(query_params)
        history_params["view"] = "history"
        recent_operations, _, _, _ = await load_operations(
            db, trader, cabinet_base=cabinet_base, query_params=history_params, view="history", limit=8
        )
        appeals = await load_appeals(db, trader, cabinet_base=cabinet_base)
        if selected_operation_id:
            selected = await load_operation_detail(
                db, trader, cabinet_base=cabinet_base, operation_id=selected_operation_id
            )
    elif section == "history":
        recent_operations, requisites, filters, aggregates = await load_operations(
            db, trader, cabinet_base=cabinet_base, query_params=query_params, view="history", limit=500
        )
    elif section == "requisites":
        requisites = await _own_requisites(db, trader.id)
    elif section == "finance":
        ledger = await load_ledger(db, trader.id)
    elif section == "disputes":
        appeals = await load_appeals(db, trader, cabinet_base=cabinet_base)
    elif section == "analytics":
        analytics = await load_analytics(db, trader)

    requisite_views = tuple(_requisite_view(row, cabinet_base=cabinet_base) for row in requisites)
    return TraderPageData(
        balance=_money(trader.trader_balance),
        hold=_money(trader.trader_hold),
        available=_money(reconciliation.actual_available),
        finance_reconciled=reconciliation.ok,
        traffic_status=str(trader.trader_traffic_status or ""),
        traffic_label=_traffic_label(str(trader.trader_traffic_status or "")),
        active_operations=active_operations,
        recent_operations=recent_operations,
        requisites=requisite_views,
        ledger=ledger,
        appeals=appeals,
        aggregates=aggregates,
        filters=filters,
        analytics=analytics,
        selected_operation=selected,
        active_appeal_count=sum(1 for row in appeals if row.status in _ACTIVE_APPEAL_STATUSES),
        unavailable_requisite_count=sum(1 for row in requisite_views if not row.is_available),
        banks=tuple({"code": row.code, "label": row.display_name} for row in get_enabled_banks()),
        operators=tuple({"code": row.code, "label": row.display_name} for row in get_enabled_mobile_operators()),
        rejection_reasons=dict(TRADER_REJECTION_REASONS),
    )


