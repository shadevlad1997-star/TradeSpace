from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any


_SENSITIVE_KEY_PARTS = (
    "secret",
    "token",
    "password",
    "authorization",
    "cookie",
    "api_key",
    "apikey",
    "signature",
    "hmac",
)
_SENSITIVE_TEXT = re.compile(
    r"(?i)\b(authorization|cookie|set-cookie|x-api-key|api[_-]?key|"
    r"webhook[_-]?secret|secret|token|signature)\b\s*[:=]\s*([^\s,;]+)"
)


@dataclass(frozen=True, slots=True)
class WebhookAttemptView:
    attempt_no: int
    status: str
    status_code: int | None
    error: str
    response_snippet: str
    created_at: datetime | None
    updated_at: datetime | None


def _sensitive_key(value: object) -> bool:
    normalized = str(value).lower().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[redacted]" if _sensitive_key(key) else redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return _SENSITIVE_TEXT.sub(lambda match: f"{match.group(1)}: [redacted]", value)
    return value


def safe_json(value: Any) -> str:
    return json.dumps(redact_sensitive(value), ensure_ascii=False, indent=2, default=str)


def sanitize_response_snippet(value: object, *, limit: int = 1000) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        clean = redact_sensitive(raw)
    else:
        clean = json.dumps(redact_sensitive(parsed), ensure_ascii=False, indent=2, default=str)
    return str(clean)[:limit]


def build_attempts_by_event_id(rows: list[object]) -> dict[object, list[WebhookAttemptView]]:
    result: dict[object, list[WebhookAttemptView]] = {}
    for row in rows:
        result.setdefault(row.webhook_event_id, []).append(
            WebhookAttemptView(
                attempt_no=int(row.attempt_no or 0),
                status=str(row.status or ""),
                status_code=row.status_code,
                error=sanitize_response_snippet(row.error, limit=500),
                response_snippet=sanitize_response_snippet(row.response_snippet),
                created_at=getattr(row, "created_at", None),
                updated_at=getattr(row, "updated_at", None),
            )
        )
    return result


_OPERATION_LABELS = {"deposit": "Пополнение", "payout": "Выплата"}
_WEBHOOK_STATUS_LABELS = {
    "queued": "В очереди",
    "pending": "Ожидает",
    "retry": "Повторная попытка",
    "delivered": "Доставлен",
    "failed": "Ошибка",
    "dead": "Исчерпан лимит попыток",
}
_DEPOSIT_STATUS_LABELS = {
    "created": "Создано",
    "pending": "Ожидает обработки",
    "appeal_opened": "Открыта апелляция",
    "paid": "Оплачено",
    "failed": "Не оплачено",
    "expired": "Истёк срок",
    "cancelled": "Отменено",
}
_FAILURE_REASON_LABELS = {
    "trader_timeout": "Тайм-аут трейдера",
    "legacy_missing_hold": "Нет резервирования",
}
_TRAFFIC_STATUS_LABELS = {
    "active": "Активен",
    "under_review": "Под наблюдением",
    "reinstated_limited": "Ограничен",
    "auto_paused": "Приостановлен автоматически",
    "manual_paused": "Приостановлен вручную",
    "reinstated_full": "Полностью восстановлен",
    "blocked": "Заблокирован",
}


def short_identifier(value: object, *, head: int = 8, tail: int = 6) -> str:
    text = str(value or "")
    if len(text) <= head + tail + 1:
        return text or "—"
    return f"{text[:head]}…{text[-tail:]}"


def money_rub(value: object) -> str:
    try:
        amount = Decimal(str(value or "0")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return "—"
    rendered = f"{amount:,.2f}".replace(",", " ").replace(".", ",")
    if rendered.endswith(",00"):
        rendered = rendered[:-3]
    return f"{rendered} ₽"


def deposit_status_label(value: object) -> str:
    return _DEPOSIT_STATUS_LABELS.get(str(value or ""), "Системный статус")


def failure_reason_label(value: object) -> str:
    raw = str(value or "")
    return _FAILURE_REASON_LABELS.get(raw, "Причина указана в деталях" if raw else "—")


def traffic_status_label(value: object) -> str:
    return _TRAFFIC_STATUS_LABELS.get(str(value or ""), "Неизвестное состояние")


def traffic_status_class(value: object) -> str:
    raw = str(value or "")
    if raw in {"active", "reinstated_full"}:
        return "status-ok"
    if raw in {"under_review", "reinstated_limited"}:
        return "status-wait"
    if raw in {"auto_paused", "manual_paused", "blocked"}:
        return "status-bad"
    return "status-neutral"


def webhook_status_label(value: object) -> str:
    return _WEBHOOK_STATUS_LABELS.get(str(value or ""), "Системный статус")


def webhook_status_class(value: object) -> str:
    raw = str(value or "")
    if raw == "delivered":
        return "status-ok"
    if raw in {"queued", "pending", "retry"}:
        return "status-wait"
    if raw in {"failed", "dead"}:
        return "status-bad"
    return "status-neutral"


def build_webhook_event_view(event: object, attempts: list[WebhookAttemptView]) -> dict[str, Any]:
    payload = event.payload if isinstance(event.payload, dict) else {}
    operation_type = str(payload.get("operation_type") or str(event.event_type or "").split(".", 1)[0])
    external_id = str(payload.get("external_id") or "")
    amount = payload.get("amount")
    currency = str(payload.get("currency") or "")
    return {
        "id": str(event.id),
        "event_type": str(event.event_type or ""),
        "object_type": _OPERATION_LABELS.get(operation_type, operation_type or "Событие"),
        "object_status": deposit_status_label(payload.get("status")),
        "external_id": external_id,
        "external_id_short": short_identifier(external_id),
        "amount": str(amount or ""),
        "currency": currency,
        "status": str(event.status or ""),
        "status_label": webhook_status_label(event.status),
        "status_class": webhook_status_class(event.status),
        "attempt_count": int(event.attempts or 0),
        "last_error": sanitize_response_snippet(event.last_error, limit=500),
        "last_status_code": event.last_status_code,
        "created_at": event.created_at,
        "correlation_id": str(event.correlation_id or ""),
        "payload_json": safe_json(payload),
        "attempts": [asdict(attempt) for attempt in attempts],
    }


_AUDIT_LABELS = {
    "successful_login": "Успешный вход",
    "logout": "Выход из кабинета",
    "2fa_success": "Успешная проверка 2FA",
    "requisite_updated": "Реквизит обновлён",
    "deposit_expired_by_ttl": "Заявка закрыта по тайм-ауту",
    "deposit_confirmed_from_cabinet": "Депозит подтверждён",
    "trader_finance_updated": "Изменены финансовые настройки трейдера",
}


def audit_event_label(action: object, details: object) -> str:
    raw_action = str(action or "")
    if raw_action == "requisite_toggled" and isinstance(details, dict):
        enabled = details.get("enabled")
        if enabled is True:
            return "Реквизит включён"
        if enabled is False:
            return "Реквизит отключён"
    return _AUDIT_LABELS.get(raw_action, "Системное событие")


def build_audit_view(row: object, actor_name: str) -> dict[str, Any]:
    details = row.details if isinstance(row.details, dict) else {}
    return {
        "id": str(row.id),
        "created_at": row.created_at,
        "actor": actor_name,
        "event_label": audit_event_label(row.action, details),
        "raw_action": str(row.action or ""),
        "target_type": str(row.target_type or ""),
        "target_id": str(row.target_id or ""),
        "target_id_short": short_identifier(row.target_id),
        "ip": str(row.ip or ""),
        "request_id": str(details.get("request_id") or ""),
        "old_status": str(details.get("old_status") or details.get("old") or ""),
        "new_status": str(details.get("new_status") or details.get("new") or ""),
        "reason": str(details.get("reason") or ""),
        "details_json": safe_json(details),
    }
