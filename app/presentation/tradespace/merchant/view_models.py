from __future__ import annotations
from app.presentation.tradespace.financial_copy import financial_description


import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import AppealStatus, DepositStatus
from app.presentation.tradespace.labels import reason_label
from app.core.payment_methods import PAYMENT_METHOD_OPTIONS, payment_method_label
from app.core.requisite_providers import requisite_provider_display
from app.models import (
    ApiKey, Appeal, AppealMessage, Balance, Deposit, LedgerEntry, Merchant,
    MerchantRollingAllocation, MerchantRollingLedgerEntry, MerchantRollingTransfer,
    MerchantSettlement, MerchantWebhookSigningKey, OperationFeeSnapshot, Payout,
    Requisite, WebhookDeliveryAttempt, WebhookEvent,
)
from app.services.appeals import appeal_deadline, appeal_remaining_seconds, metadata as appeal_metadata
from app.services.merchant_api_keys import api_key_fingerprint, api_key_mode_label
from app.services.rolling import rolling_overview
from app.services.settlements import merchant_settlement_quote
from app.web.view_models import build_attempts_by_event_id

ACTIVE_DEPOSITS = frozenset({"created", "pending", "appeal_opened"})
TERMINAL_DEPOSITS = frozenset({"paid", "failed", "expired", "cancelled"})
ACTIVE_APPEALS = frozenset({"opened", "in_review"})
PAGE_SIZE = 25

DEPOSIT_STATUS = {
    "created": ("Создана", "neutral"), "pending": ("Ожидает оплаты", "attention"),
    "paid": ("Оплачена", "positive"), "failed": ("Не оплачена", "critical"),
    "expired": ("Срок истёк", "critical"), "cancelled": ("Отменена", "neutral"),
    "appeal_opened": ("Открыт спор", "attention"),
}
PAYOUT_STATUS = {
    "created": ("Создана", "neutral"), "pending": ("Ожидает", "attention"),
    "processing": ("В обработке", "attention"), "completed": ("Выполнена", "positive"),
    "failed": ("Ошибка", "critical"), "cancelled": ("Отменена", "neutral"),
    "rejected": ("Отклонена", "critical"), "appeal_opened": ("Открыт спор", "attention"),
}
SETTLEMENT_STATUS = {
    "pending": ("На рассмотрении", "attention"), "completed": ("Выполнен", "positive"),
    "rejected": ("Отклонён", "critical"),
}
APPEAL_STATUS = {
    "opened": ("Открыт", "attention"), "in_review": ("На проверке", "attention"),
    "approved": ("Одобрен", "positive"), "rejected": ("Отклонён", "critical"),
    "returned_to_processing": ("Возвращён в работу", "neutral"),
    "closed": ("Закрыт", "neutral"),
}
WEBHOOK_STATUS = {
    "processing": ("Отправляется", "attention"),
    "configuration_required": ("Требуется настройка", "critical"),
    "queued": ("В очереди", "attention"), "pending": ("Ожидает", "attention"),
    "retry": ("Повтор", "attention"), "delivered": ("Доставлен", "positive"),
    "failed": ("Ошибка", "critical"), "dead": ("Не доставлен", "critical"),
}
ROLLING_STATUS = {
    "active": ("Активен", "positive"), "exhausted": ("Погашен", "neutral"),
    "suspended": ("Приостановлен", "critical"),
    "pending_confirmation": ("Ждёт подтверждения", "attention"),
    "confirmed": ("Подтверждён", "positive"), "disputed": ("Оспорен", "critical"),
    "cancelled": ("Отменён", "neutral"), "absent": ("Не подключён", "neutral"),
}
LEDGER_LABELS = {
    "credit": "Зачисление", "debit": "Списание", "hold": "Резервирование",
    "release": "Освобождение резерва", "fee": "Комиссия",
}


def money(value: Any, places: str = "0.01") -> str:
    try:
        parsed = Decimal(str(value if value is not None else "0"))
        if not parsed.is_finite():
            raise InvalidOperation
        return f"{parsed.quantize(Decimal(places))}"
    except (InvalidOperation, ValueError, TypeError):
        return f"{Decimal('0').quantize(Decimal(places))}"


def short_id(value: Any, head: int = 8, tail: int = 6) -> str:
    text = str(value or "")
    return text if len(text) <= head + tail + 1 else f"{text[:head]}…{text[-tail:]}"


def state(mapping: dict[str, tuple[str, str]], value: Any) -> tuple[str, str]:
    return mapping.get(str(value or ""), ("Статус не определён", "neutral"))


def display_requisite(row: Requisite | None) -> str:
    if not row:
        return ""
    from app.web.routes import display_requisite_value
    return display_requisite_value(row)


@dataclass(frozen=True, slots=True)
class OperationView:
    id: str
    short_id: str
    external_id: str
    external_short: str
    operation_type: str
    type_label: str
    amount: str
    currency: str
    method: str
    method_label: str
    status: str
    status_label: str
    status_tone: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None
    requisite: str = ""
    provider_name: str = ""
    recipient_name: str = ""
    fee_amount: str = "0.00"
    fee_percent: str = "0.0000"
    payable_amount: str = "0.00"
    failure_reason: str = ""
    detail_url: str = ""
    trader_id: str = ""
    has_active_appeal: bool = False
    webhook_label: str = "Нет события"
    webhook_tone: str = "neutral"


@dataclass(frozen=True, slots=True)
class LedgerView:
    created_at: datetime
    entry_type: str
    entry_label: str
    amount: str
    currency: str
    operation_id: str
    operation_short: str
    description: str
    reference_label: str = ""


@dataclass(frozen=True, slots=True)
class SettlementView:
    id: str
    short_id: str
    status: str
    status_label: str
    status_tone: str
    amount_usdt: str
    fee_usdt: str
    rate_rub: str
    amount_rub: str
    fee_rub: str
    total_debit_rub: str
    address: str
    address_short: str
    network: str
    tx_hash: str
    tx_short: str
    reject_reason: str
    created_at: datetime
    processed_at: datetime | None


@dataclass(frozen=True, slots=True)
class RollingTransferView:
    id: str
    sequence_no: int
    status: str
    status_label: str
    status_tone: str
    amount_usdt: str
    recovered_usdt: str
    remaining_usdt: str
    network: str
    address: str
    address_short: str
    tx_hash: str
    tx_short: str
    sent_at: datetime
    confirmed_at: datetime | None
    comment: str
    dispute_reason: str
    can_respond: bool
    confirm_url: str
    dispute_url: str


@dataclass(frozen=True, slots=True)
class RollingLedgerView:
    created_at: datetime
    entry_type: str
    label: str
    amount_usdt: str
    amount_rub: str
    rate_rub: str
    deposit_id: str
    deposit_short: str
    reason: str


@dataclass(frozen=True, slots=True)
class WebhookView:
    id: str
    short_id: str
    event_type: str
    external_id: str
    external_short: str
    operation_status: str
    amount: str
    currency: str
    status: str
    status_label: str
    status_tone: str
    attempts: int
    max_attempts: int
    last_status_code: int | None
    next_attempt_at: datetime | None
    created_at: datetime
    attempt_rows: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class AppealView:
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
    receipt_url: str
    receipt_name: str
    messages: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class MerchantPageData:
    merchant_id: str
    merchant_name: str
    sandbox_mode: bool
    webhook_url: str
    ip_whitelist: tuple[str, ...]
    available: str
    frozen: str
    operations: tuple[OperationView, ...]
    recent_operations: tuple[OperationView, ...]
    selected_operation: OperationView | None
    payouts: tuple[OperationView, ...]
    ledger: tuple[LedgerView, ...]
    settlements: tuple[SettlementView, ...]
    rolling: dict[str, Any]
    rolling_transfers: tuple[RollingTransferView, ...]
    rolling_ledger: tuple[RollingLedgerView, ...]
    rolling_allocations: tuple[dict[str, Any], ...]
    webhooks: tuple[WebhookView, ...]
    api_keys: tuple[dict[str, Any], ...]
    signing_keys: tuple[dict[str, Any], ...]
    appeals: tuple[AppealView, ...]
    appeal_candidates: tuple[OperationView, ...]
    analytics: dict[str, Any]
    filters: dict[str, str]
    page: int
    page_count: int
    total_filtered: int
    settlement_quote: dict[str, Any]
    settlement_idempotency_key: str
    attention_count: int
    webhook_issue_count: int
    pending_settlement_count: int
    active_appeal_count: int
    payment_methods: tuple[dict[str, str], ...] = field(default_factory=tuple)
    integration_state: dict[str, Any] = field(default_factory=dict)


async def _merchant(db: AsyncSession, user_id: UUID) -> Merchant | None:
    return await db.scalar(select(Merchant).where(
        Merchant.owner_id == user_id, Merchant.is_archived.is_(False)
    ))


async def _load_operations(db: AsyncSession, merchant: Merchant, cabinet_base: str, query: Any):
    deposits = list((await db.scalars(
        select(Deposit).where(Deposit.merchant_id == merchant.id)
        .order_by(Deposit.created_at.desc(), Deposit.id.desc()).limit(2000)
    )).all())
    ids = [row.id for row in deposits]
    req_ids = {row.requisites_id for row in deposits if row.requisites_id}
    reqs = list((await db.scalars(select(Requisite).where(Requisite.id.in_(req_ids)))).all()) if req_ids else []
    req_by_id = {row.id: row for row in reqs}
    snapshots = list((await db.scalars(
        select(OperationFeeSnapshot).where(OperationFeeSnapshot.deposit_id.in_(ids))
    )).all()) if ids else []
    snapshot_by_id = {row.deposit_id: row for row in snapshots}
    active_appeals = list((await db.scalars(select(Appeal).where(
        Appeal.operation_type == "deposit", Appeal.operation_id.in_(ids),
        Appeal.status.in_(list(ACTIVE_APPEALS)),
    ))).all()) if ids else []
    appealed = {row.operation_id for row in active_appeals}
    events = list((await db.scalars(
        select(WebhookEvent).where(WebhookEvent.merchant_id == merchant.id)
        .order_by(WebhookEvent.created_at.desc()).limit(1000)
    )).all())
    event_by_external: dict[str, WebhookEvent] = {}
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        external = str(payload.get("external_id") or "")
        if external:
            event_by_external.setdefault(external, event)

    all_rows: list[OperationView] = []
    for deposit in deposits:
        req = req_by_id.get(deposit.requisites_id)
        snapshot = snapshot_by_id.get(deposit.id)
        meta = deposit.metadata_json if isinstance(deposit.metadata_json, dict) else {}
        label, tone = state(DEPOSIT_STATUS, deposit.status)
        event = event_by_external.get(str(deposit.external_id or ""))
        event_label, event_tone = state(WEBHOOK_STATUS, event.status if event else "")
        payable = meta.get("merchant_payable_amount", meta.get("merchant_net_amount", "0"))
        provider = requisite_provider_display(
            req.method, req.bank_code, req.operator_code, req.bank_name
        ) if req else ""
        all_rows.append(OperationView(
            id=str(deposit.id), short_id=short_id(deposit.id),
            external_id=str(deposit.external_id or ""), external_short=short_id(deposit.external_id),
            operation_type="deposit", type_label="Пополнение", amount=money(deposit.amount),
            currency=str(deposit.currency or "RUB"), method=str(deposit.method or ""),
            method_label=payment_method_label(deposit.method), status=str(deposit.status),
            status_label=label, status_tone=tone, created_at=deposit.created_at,
            updated_at=deposit.updated_at, expires_at=deposit.expires_at,
            requisite=display_requisite(req), provider_name=provider,
            recipient_name=str((req.full_name or req.owner_name) if req else ""),
            fee_amount=money(snapshot.merchant_fee_amount if snapshot else meta.get("merchant_fee_amount", "0")),
            fee_percent=money(snapshot.merchant_rate_percent if snapshot else merchant.merchant_commission_percent, "0.0001"),
            payable_amount=money(payable), failure_reason=reason_label(meta.get("failure_reason")),
            detail_url=f"{cabinet_base}/tradespace/operations/{deposit.id}",
            trader_id=str(req.trader_id if req else ""), has_active_appeal=deposit.id in appealed,
            webhook_label=event_label if event else "Нет события",
            webhook_tone=event_tone if event else "neutral",
        ))

    filters = {key: str(query.get(key) or "").strip() for key in ("query", "status", "payment_method", "date")}
    if filters["status"] not in DEPOSIT_STATUS:
        filters["status"] = ""
    if filters["payment_method"] not in {str(value) for value, _ in PAYMENT_METHOD_OPTIONS}:
        filters["payment_method"] = ""
    filtered = all_rows
    if filters["query"]:
        needle = filters["query"].casefold()
        filtered = [row for row in filtered if needle in row.id.casefold() or needle in row.external_id.casefold() or needle in row.requisite.casefold()]
    if filters["status"]:
        filtered = [row for row in filtered if row.status == filters["status"]]
    if filters["payment_method"]:
        filtered = [row for row in filtered if row.method == filters["payment_method"]]
    if filters["date"]:
        filtered = [row for row in filtered if row.created_at.date().isoformat() == filters["date"]]
    try:
        page = max(1, int(query.get("page") or 1))
    except (TypeError, ValueError):
        page = 1
    pages = max(1, math.ceil(len(filtered) / PAGE_SIZE))
    page = min(page, pages)
    paged = filtered[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]

    payouts = []
    payout_rows = list((await db.scalars(select(Payout).where(
        Payout.merchant_id == merchant.id
    ).order_by(Payout.created_at.desc()).limit(300))).all())
    for payout in payout_rows:
        label, tone = state(PAYOUT_STATUS, payout.status)
        payouts.append(OperationView(
            id=str(payout.id), short_id=short_id(payout.id), external_id=str(payout.external_id),
            external_short=short_id(payout.external_id), operation_type="payout", type_label="Выплата",
            amount=money(payout.amount), currency=str(payout.currency or "RUB"),
            method=str(payout.method), method_label=payment_method_label(payout.method),
            status=str(payout.status), status_label=label, status_tone=tone,
            created_at=payout.created_at, updated_at=payout.updated_at, expires_at=None,
        ))
    return all_rows, paged, payouts, filters, page, pages, len(filtered)


async def _load_finance(db: AsyncSession, merchant_id: UUID, cabinet_base: str):
    balance = await db.scalar(select(Balance).where(
        Balance.merchant_id == merchant_id, Balance.currency == "RUB"
    ))
    ledger_rows = list((await db.scalars(select(LedgerEntry).where(
        LedgerEntry.merchant_id == merchant_id
    ).order_by(LedgerEntry.created_at.desc(), LedgerEntry.id.desc()).limit(300))).all())
    reference_ids = {row.operation_id for row in ledger_rows if row.operation_id}
    owned_deposits = set((await db.scalars(select(Deposit.id).where(Deposit.id.in_(reference_ids), Deposit.merchant_id == merchant_id))).all())
    reference_labels = {identifier: 'Операция' for identifier in owned_deposits}
    for model, label in ((Payout,'Выплата'),(MerchantSettlement,'Расчёт')):
        owned = (await db.scalars(select(model.id).where(model.id.in_(reference_ids),model.merchant_id==merchant_id))).all()
        reference_labels.update({identifier:label for identifier in owned})
    ledger = tuple(LedgerView(
        created_at=row.created_at, entry_type=str(row.entry_type),
        entry_label=LEDGER_LABELS.get(str(row.entry_type), "Финансовое движение"),
        amount=money(row.amount), currency=str(row.currency or "RUB"),
        operation_id=str(row.operation_id) if row.operation_id in owned_deposits else "", operation_short=short_id(row.operation_id),
        description=financial_description(row.description, user_comment=str(row.idempotency_key or "").startswith("manual")),
        reference_label=reference_labels.get(row.operation_id,""),
    ) for row in ledger_rows)

    overview = await rolling_overview(db, merchant_id)
    label, tone = state(ROLLING_STATUS, overview.get("status"))
    overview = {**overview, "principal": money(overview.get("principal"), "0.000001"),
        "recovered": money(overview.get("recovered"), "0.000001"),
        "outstanding": money(overview.get("outstanding"), "0.000001"),
        "pending_exposure": money(overview.get("pending_exposure"), "0.000001"),
        "status_label": label, "status_tone": tone}

    transfer_rows = list((await db.scalars(select(MerchantRollingTransfer).where(
        MerchantRollingTransfer.merchant_id == merchant_id
    ).order_by(MerchantRollingTransfer.sequence_no.desc()).limit(200))).all())
    transfers = []
    for row in transfer_rows:
        label, tone = state(ROLLING_STATUS, row.status)
        transfers.append(RollingTransferView(
            id=str(row.id), sequence_no=int(row.sequence_no), status=str(row.status),
            status_label=label, status_tone=tone, amount_usdt=money(row.amount_usdt, "0.000001"),
            recovered_usdt=money(row.recovered_usdt, "0.000001"),
            remaining_usdt=money(row.remaining_usdt, "0.000001"),
            network=str(row.network or ""), address=str(row.destination_address or ""),
            address_short=short_id(row.destination_address, 10, 8), tx_hash=str(row.tx_hash or ""),
            tx_short=short_id(row.tx_hash, 10, 8), sent_at=row.sent_at,
            confirmed_at=row.confirmed_at, comment=str(row.comment or ""),
            dispute_reason=str(row.dispute_reason or ""),
            can_respond=str(row.status) == "pending_confirmation",
            confirm_url=f"{cabinet_base}/rolling/transfers/{row.id}/confirm",
            dispute_url=f"{cabinet_base}/rolling/transfers/{row.id}/dispute",
        ))

    rolling_labels = {"funding": "Финансирование", "topup": "Пополнение Rolling",
        "pending_added": "Ожидающее обязательство", "pending_released": "Освобождение обязательства",
        "recovery": "Погашение", "settle_overflow": "Зачисление в доступный баланс",
        "reversal": "Возврат", "manual_adjustment": "Ручная корректировка", "write_off": "Списание"}
    rolling_rows = list((await db.scalars(select(MerchantRollingLedgerEntry).where(
        MerchantRollingLedgerEntry.merchant_id == merchant_id
    ).order_by(MerchantRollingLedgerEntry.created_at.desc()).limit(200))).all())
    rolling_ledger = tuple(RollingLedgerView(
        created_at=row.created_at, entry_type=str(row.entry_type),
        label=rolling_labels.get(str(row.entry_type), "Движение Rolling"),
        amount_usdt=money(row.amount_usdt, "0.000001") if row.amount_usdt is not None else "",
        amount_rub=money(row.amount_rub) if row.amount_rub is not None else "",
        rate_rub=money(row.rate_rub, "0.00000001") if row.rate_rub is not None else "",
        deposit_id=str(row.deposit_id or ""), deposit_short=short_id(row.deposit_id),
        reason=financial_description(row.reason,user_comment=row.entry_type in {"manual_adjustment","write_off","reversal"}),
    ) for row in rolling_rows)

    allocation_rows = list((await db.scalars(select(MerchantRollingAllocation).where(
        MerchantRollingAllocation.merchant_id == merchant_id
    ).order_by(MerchantRollingAllocation.created_at.desc()).limit(100))).all())
    allocations = tuple({
        "deposit_id": str(row.deposit_id), "deposit_short": short_id(row.deposit_id),
        "operation_url": f"{cabinet_base}/tradespace/operations/{row.deposit_id}",
        "status": str(row.status), "eligibility_status": str(row.eligibility_status),
        "gross_rub": money(row.gross_rub), "fee_rub": money(row.merchant_fee_rub),
        "payable_rub": money(row.merchant_payable_rub),
        "rolling_usdt": money(row.rolling_applied_usdt, "0.000001"),
        "rolling_rub": money(row.rolling_applied_rub), "settle_rub": money(row.settle_credited_rub),
        "rate_rub": money(row.rapira_rate_rub, "0.00000001"), "created_at": row.created_at,
    } for row in allocation_rows)
    return money(balance.available if balance else 0), money(balance.frozen if balance else 0), ledger, overview, tuple(transfers), rolling_ledger, allocations


async def _load_settlements(db: AsyncSession, merchant_id: UUID):
    rows = list((await db.scalars(select(MerchantSettlement).where(
        MerchantSettlement.merchant_id == merchant_id
    ).order_by(MerchantSettlement.created_at.desc()).limit(200))).all())
    result = []
    for row in rows:
        label, tone = state(SETTLEMENT_STATUS, row.status)
        result.append(SettlementView(
            id=str(row.id), short_id=short_id(row.id), status=str(row.status),
            status_label=label, status_tone=tone, amount_usdt=money(row.amount_usdt),
            fee_usdt=money(row.fee_usdt), rate_rub=money(row.rate_rub, "0.0001"),
            amount_rub=money(row.amount_rub), fee_rub=money(row.fee_rub),
            total_debit_rub=money(row.total_debit_rub), address=str(row.trc20_address),
            address_short=short_id(row.trc20_address, 10, 8), network=str(row.network),
            tx_hash=str(row.tx_hash or ""), tx_short=short_id(row.tx_hash, 10, 8),
            reject_reason=str(row.reject_reason or ""), created_at=row.created_at,
            processed_at=row.processed_at,
        ))
    return tuple(result)


async def _load_integration(db: AsyncSession, merchant: Merchant):
    key_rows = list((await db.scalars(select(ApiKey).where(
        ApiKey.merchant_id == merchant.id
    ).order_by(ApiKey.is_active.desc(), ApiKey.created_at.desc()).limit(30))).all())
    keys = tuple({
        "id": str(row.id), "fingerprint": api_key_fingerprint(row.api_key),
        "api_key": str(row.api_key), "mode": str(row.mode),
        "mode_label": api_key_mode_label(row.mode), "active": bool(row.is_active),
        "last_used_at": row.last_used_at, "created_at": row.created_at,
    } for row in key_rows)

    signing_rows = list((await db.scalars(select(MerchantWebhookSigningKey).where(
        MerchantWebhookSigningKey.merchant_id == merchant.id
    ).order_by(MerchantWebhookSigningKey.created_at.desc()).limit(30))).all())
    signing = tuple({
        "key_id": str(row.key_id), "fingerprint": short_id(row.key_id),
        "status": str(row.status),
        "status_label": {"active": "Активен", "retiring": "Завершается", "revoked": "Отозван"}.get(str(row.status), "Статус не определён"),
        "status_tone": {"active": "positive", "retiring": "attention", "revoked": "critical"}.get(str(row.status), "neutral"),
        "retire_at": row.retire_at, "created_at": row.created_at,
    } for row in signing_rows)

    events = list((await db.scalars(select(WebhookEvent).where(
        WebhookEvent.merchant_id == merchant.id
    ).order_by(WebhookEvent.created_at.desc()).limit(300))).all())
    event_ids = [row.id for row in events]
    attempt_rows = list((await db.scalars(select(WebhookDeliveryAttempt).where(
        WebhookDeliveryAttempt.webhook_event_id.in_(event_ids)
    ).order_by(WebhookDeliveryAttempt.webhook_event_id, WebhookDeliveryAttempt.attempt_no))).all()) if event_ids else []
    attempts = build_attempts_by_event_id(attempt_rows)
    webhooks = []
    for row in events:
        payload = row.payload if isinstance(row.payload, dict) else {}
        label, tone = state(WEBHOOK_STATUS, row.status)
        safe_attempts = tuple({
            "attempt_no": int(attempt.attempt_no), "status": str(attempt.status),
            "status_code": attempt.status_code, "created_at": attempt.created_at,
        } for attempt in attempts.get(row.id, []))
        webhooks.append(WebhookView(
            id=str(row.id), short_id=short_id(row.id), event_type=str(row.event_type or ""),
            external_id=str(payload.get("external_id") or ""),
            external_short=short_id(payload.get("external_id")),
            operation_status=str(payload.get("status") or ""),
            amount=money(payload.get("amount")) if payload.get("amount") is not None else "",
            currency=str(payload.get("currency") or ""), status=str(row.status),
            status_label=label, status_tone=tone, attempts=int(row.attempts or 0),
            max_attempts=int(row.max_attempts or 0), last_status_code=row.last_status_code,
            next_attempt_at=row.next_attempt_at or row.next_retry_at, created_at=row.created_at,
            attempt_rows=safe_attempts,
        ))
    return keys, signing, tuple(webhooks)


async def _load_appeals(db: AsyncSession, merchant_id: UUID, user_id: UUID, operations: list[OperationView], cabinet_base: str):
    deposits = list((await db.scalars(select(Deposit).where(Deposit.merchant_id == merchant_id))).all())
    dep_by_id = {row.id: row for row in deposits}
    if not dep_by_id:
        return (), ()
    rows = list((await db.scalars(select(Appeal).where(
        Appeal.operation_type == "deposit", Appeal.operation_id.in_(list(dep_by_id))
    ).order_by(Appeal.created_at.desc()).limit(500))).all())
    messages = list((await db.scalars(select(AppealMessage).where(
        AppealMessage.appeal_id.in_([row.id for row in rows])
    ).order_by(AppealMessage.created_at.asc()))).all()) if rows else []
    message_map: dict[UUID, list[dict[str, Any]]] = {}
    for msg in messages:
        message_map.setdefault(msg.appeal_id, []).append({
            "created_at": msg.created_at,
            "author": "Вы" if str(msg.author_id or "") == str(user_id) else "Участник / система",
            "message": str(msg.message or ""), "has_attachment": bool(msg.attachment_path),
        })
    result = []
    active_ids = {row.operation_id for row in rows if row.status in ACTIVE_APPEALS}
    for appeal in rows:
        deposit = dep_by_id.get(appeal.operation_id)
        if not deposit:
            continue
        meta = appeal_metadata(appeal)
        label, tone = state(APPEAL_STATUS, appeal.status)
        receipt = meta.get("receipt_file") if isinstance(meta.get("receipt_file"), dict) else {}
        path = str(receipt.get("path") or "")
        result.append(AppealView(
            id=str(appeal.id), short_id=short_id(appeal.id),
            operation_id=str(deposit.id), operation_short=short_id(deposit.id),
            operation_url=f"{cabinet_base}/tradespace/operations/{deposit.id}",
            status=str(appeal.status), status_label=label, status_tone=tone,
            decision=str(appeal.decision or ""), amount=money(meta.get("amount_claimed") or deposit.amount),
            currency=str(deposit.currency or "RUB"), requisite=str(meta.get("requisite") or ""),
            provider_name=str(meta.get("recipient_bank") or ""), created_at=appeal.created_at,
            deadline_at=appeal_deadline(appeal), remaining_seconds=appeal_remaining_seconds(appeal),
            receipt_url=f"{cabinet_base}/appeals/files/{path}" if path else "",
            receipt_name=str(receipt.get("original_name") or "Чек"),
            messages=tuple(message_map.get(appeal.id, ())),
        ))
    candidates = tuple(row for row in operations if UUID(row.id) not in active_ids and row.trader_id)
    return tuple(result), candidates[:100]


def _analytics(operations: list[OperationView]) -> dict[str, Any]:
    paid = [row for row in operations if row.status == "paid"]
    failed = [row for row in operations if row.status in TERMINAL_DEPOSITS and row.status != "paid"]
    active = [row for row in operations if row.status in ACTIVE_DEPOSITS]
    methods: dict[str, dict[str, Any]] = {}
    statuses: dict[str, dict[str, Any]] = {}
    for row in operations:
        method = methods.setdefault(row.method, {"label": row.method_label, "count": 0, "paid": 0, "paid_volume": Decimal("0")})
        method["count"] += 1
        if row.status == "paid":
            method["paid"] += 1
            method["paid_volume"] += Decimal(row.amount)
        status = statuses.setdefault(row.status, {"label": row.status_label, "tone": row.status_tone, "count": 0, "volume": Decimal("0")})
        status["count"] += 1
        status["volume"] += Decimal(row.amount)
    return {
        "total_count": len(operations), "active_count": len(active), "paid_count": len(paid),
        "failed_count": len(failed),
        "paid_volume": money(sum((Decimal(row.amount) for row in paid), Decimal("0"))),
        "fee_total": money(sum((Decimal(row.fee_amount) for row in paid), Decimal("0"))),
        "payable_total": money(sum((Decimal(row.payable_amount) for row in paid), Decimal("0"))),
        "method_rows": tuple({**row, "paid_volume": money(row["paid_volume"])} for row in sorted(methods.values(), key=lambda item: -item["count"])),
        "status_rows": tuple({**row, "volume": money(row["volume"])} for row in sorted(statuses.values(), key=lambda item: -item["count"])),
    }


async def load_merchant_page(db: AsyncSession, user: Any, *, cabinet_base: str, query_params: Any, section: str, selected_operation_id: UUID | None = None) -> MerchantPageData | None:
    merchant = await _merchant(db, user.id)
    if not merchant:
        return None
    all_ops, operations, payouts, filters, page, pages, total = await _load_operations(db, merchant, cabinet_base, query_params)
    selected = next((row for row in all_ops if row.id == str(selected_operation_id)), None)
    available, frozen, ledger, rolling, transfers, rolling_ledger, allocations = await _load_finance(db, merchant.id, cabinet_base)
    settlements = await _load_settlements(db, merchant.id)
    keys, signing_keys, webhooks = await _load_integration(db, merchant)
    appeals, candidates = await _load_appeals(db, merchant.id, user.id, all_ops, cabinet_base)
    analytics = _analytics(all_ops)
    quote = {
        "rate_available": False, "rate_rub": Decimal("0"), "fee_usdt": Decimal("0"),
        "fee_rub": Decimal("0"), "available_rub": Decimal(available),
        "pending_rub": Decimal("0"), "max_request_usdt": Decimal("0"),
        "rate_source": "not_requested", "rate_updated_at": None, "rate_stale": True,
    }
    if section == "settlements":
        quote = await merchant_settlement_quote(db, merchant.id, allow_unavailable=True)
    webhook_issues = [row for row in webhooks if row.status in {"failed", "dead", "retry"}]
    pending = [row for row in settlements if row.status == "pending"]
    active_appeals = [row for row in appeals if row.status in ACTIVE_APPEALS]
    attention = analytics["active_count"] + len(active_appeals) + len(webhook_issues) + len([row for row in transfers if row.can_respond])
    from app.services.integration_modes import integration_status
    mode_state = await integration_status(db, merchant)
    return MerchantPageData(
        merchant_id=str(merchant.id), merchant_name=str(merchant.name),
        integration_state=mode_state, sandbox_mode=mode_state["environment"] == "sandbox", webhook_url=str(merchant.webhook_url or ""),
        ip_whitelist=tuple(str(item) for item in (merchant.ip_whitelist or [])),
        available=available, frozen=frozen, operations=tuple(operations),
        recent_operations=tuple(all_ops[:8]), selected_operation=selected, payouts=tuple(payouts),
        ledger=ledger, settlements=settlements, rolling=rolling,
        rolling_transfers=transfers, rolling_ledger=rolling_ledger,
        rolling_allocations=allocations, webhooks=webhooks, api_keys=keys,
        signing_keys=signing_keys, appeals=appeals, appeal_candidates=candidates,
        analytics=analytics, filters=filters, page=page, page_count=pages,
        total_filtered=total, settlement_quote=quote,
        settlement_idempotency_key=f"merchant-settlement:{merchant.id}:{uuid.uuid4().hex}",
        attention_count=attention, webhook_issue_count=len(webhook_issues),
        pending_settlement_count=len(pending), active_appeal_count=len(active_appeals),
        payment_methods=tuple({"value": str(value), "label": label} for value, label in PAYMENT_METHOD_OPTIONS),
    )
