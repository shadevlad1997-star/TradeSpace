"""Display labels only. Never use these values for commands or state decisions."""
import re


ROLE_LABELS = {
    "operator": "Трейдер", "trader": "Трейдер", "merchant": "Мерчант",
    "teamlead": "TeamLead", "support": "Поддержка", "admin": "Администратор",
    "superadmin": "Суперадминистратор", "aggregator": "Агрегатор",
}
REASON_LABELS = {
    "merchant_cancelled": "Отменено мерчантом",
    "trader_timeout": "Время подтверждения истекло",
    "legacy_missing_hold": "Резерв средств отсутствует",
}


def reason_label(value: object) -> str:
    """Keep an operator's explanation; do not expose unknown machine reason keys."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw in REASON_LABELS:
        return REASON_LABELS[raw]
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:/-]*", raw):
        return "Причина не уточнена"
    return raw
