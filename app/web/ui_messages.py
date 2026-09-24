from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode


@dataclass(frozen=True, slots=True)
class UiMessage:
    kind: str
    section: str
    form_or_modal: str
    field: str | None
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class _MessageSpec:
    kind: str
    message: str
    section: str = ""
    form_or_modal: str = "section"
    field: str | None = None


_SPECS: dict[str, _MessageSpec] = {
    "twofa_required": _MessageSpec(
        "security",
        "Сначала подключите обязательную 2FA в разделе «Безопасность».",
        "security",
        "twofa",
    ),
    "permission_denied": _MessageSpec(
        "security", "Недостаточно прав для этого действия.", form_or_modal="action"
    ),
    "email_exists": _MessageSpec(
        "validation", "Пользователь с таким email уже существует.", "create", "user-create", "email"
    ),
    "password_too_short": _MessageSpec(
        "validation", "Новый пароль должен содержать не менее 10 символов.", "security", "password", "new_password"
    ),
    "current_password_incorrect": _MessageSpec(
        "validation", "Текущий пароль указан неверно.", "security", "password", "current_password"
    ),
    "twofa_prepare_required": _MessageSpec(
        "action", "Сначала создайте QR-код для 2FA.", "security", "twofa"
    ),
    "twofa_invalid": _MessageSpec(
        "validation", "Неверный код 2FA.", "security", "twofa", "otp"
    ),
    "twofa_disable_forbidden": _MessageSpec(
        "security", "Для этой роли 2FA обязательна и не может быть отключена.", "security", "twofa"
    ),
    "user_not_found": _MessageSpec("action", "Пользователь не найден.", "users", "user-action"),
    "user_protected": _MessageSpec("security", "Этого пользователя нельзя изменить.", "users", "user-action"),
    "merchant_not_found": _MessageSpec("action", "Мерчант не найден.", "merchants", "merchant-action"),
    "trader_not_found": _MessageSpec("action", "Трейдер не найден.", "traders", "trader-action"),
    "platform_not_found": _MessageSpec("action", "Площадка не найдена.", "merchants", "merchant-action"),
    "requisite_not_found": _MessageSpec("action", "Реквизит не найден.", "requisites", "requisite-action"),
    "requisite_scope_denied": _MessageSpec(
        "security", "Можно управлять только собственными реквизитами.", "requisites", "requisite-action"
    ),
    "requisite_delete_denied": _MessageSpec(
        "security", "Удалять реквизиты может только трейдер-владелец.", "requisites", "requisite-action"
    ),
    "requisite_deleted": _MessageSpec(
        "blocking", "Удалённый реквизит нельзя включить.", "requisites", "requisite-action"
    ),
    "trader_required": _MessageSpec(
        "validation", "Выберите трейдера для реквизита.", "requisites", "addRequisiteModal", "trader_id"
    ),
    "payment_method_invalid": _MessageSpec(
        "validation", "Выберите поддерживаемый способ оплаты.", "requisites", "requisite-form", "method"
    ),
    "bank_required": _MessageSpec(
        "validation", "Выберите банк из справочника.", "requisites", "requisite-form", "bank_code"
    ),
    "mobile_operator_required": _MessageSpec(
        "validation", "Выберите оператора связи из справочника.", "requisites", "requisite-form", "operator_code"
    ),
    "webhook_url_invalid": _MessageSpec(
        "validation", "Проверьте публичный HTTPS-адрес webhook.", "merchants", "merchant-integration", "webhook_url"
    ),
    "ip_whitelist_invalid": _MessageSpec(
        "validation", "Проверьте IP-адрес или сеть в allowlist.", "merchants", "merchant-integration", "ip_whitelist"
    ),
    "deposit_not_found": _MessageSpec("action", "Заявка не найдена.", "deposits", "deposit-action"),
    "deposit_access_denied": _MessageSpec(
        "security", "Нет доступа к обработке этой заявки.", "deposits", "deposit-action"
    ),
    "deposit_action_failed": _MessageSpec(
        "action", "Не удалось обработать заявку. Обновите страницу и повторите действие.", "deposits", "deposit-action"
    ),
    "webhook_queue_unavailable": _MessageSpec(
        "blocking", "Операция сохранена, но постановка webhook в очередь недоступна.", "deposits", "deposit-action"
    ),
    "appeal_action_failed": _MessageSpec(
        "action", "Не удалось выполнить действие с апелляцией.", "appeals", "appeal-action"
    ),
    "insufficient_balance": _MessageSpec(
        "blocking", "Недостаточно доступного баланса для операции.", "payouts", "settlement-action"
    ),
    "fee_rule_missing": _MessageSpec(
        "blocking", "Для операции не настроено обязательное правило комиссии.", "fees", "fee-rule"
    ),
    "rolling_rate_unavailable": _MessageSpec(
        "blocking", "Свежий live-курс Rapira сейчас недоступен.", "rolling", "rolling-action"
    ),
    "reconciliation_mismatch": _MessageSpec(
        "blocking", "Обнаружено расхождение reconciliation. Финансовое действие заблокировано.", "rolling", "reconciliation"
    ),
    "trc20_invalid": _MessageSpec(
        "validation", "Укажите корректный USDT TRC20-адрес.", "payouts", "settlement-action", "wallet_address"
    ),
    "assignment_overlap": _MessageSpec(
        "blocking", "На выбранную дату уже существует активное назначение.", "teamlead", "assignment"
    ),
    "teamlead_rate_unavailable": _MessageSpec(
        "blocking", "Свежий live-курс Rapira сейчас недоступен.", "teamlead", "settlement"
    ),
    "teamlead_settlement_conflict": _MessageSpec(
        "blocking", "У TeamLead уже есть незавершённый запрос на выплату.", "teamlead", "settlement"
    ),
    "teamlead_settlement_tx_hash_duplicate": _MessageSpec(
        "blocking", "Этот tx hash уже использован для завершённой выплаты.", "teamlead", "settlement", "tx_hash"
    ),
    "platform_wallet_concurrent_update": _MessageSpec(
        "action", "Кошелёк уже изменён в другой сессии. Обновите страницу.", "wallet", "platform-wallet"
    ),
    "platform_wallet_change_reason_required": _MessageSpec(
        "validation",
        "Укажите причину изменения платформенного кошелька.",
        "wallet",
        "platform-wallet",
        "change_reason",
    ),
    "ai_office_m2m_auth_required": _MessageSpec(
        "blocking", "Выберите обязательный M2M Auth type перед включением интеграции.", "ai-office", "ai-office", "auth_type"
    ),
    "ai_office_credential_required": _MessageSpec(
        "blocking", "Добавьте credential для выбранного M2M Auth type.", "ai-office", "ai-office", "credential"
    ),
    "ai_office_config_not_found": _MessageSpec(
        "action", "Конфигурация AI Office не найдена.", "ai-office", "ai-office"
    ),
    "ai_office_validation_failed": _MessageSpec(
        "validation", "Проверьте параметры подключения AI Office.", "ai-office", "ai-office"
    ),
    "ai_office_change_reason_required": _MessageSpec(
        "validation", "Укажите причину изменения конфигурации.", "ai-office", "ai-office", "change_reason"
    ),
    "ai_office_connection_failed": _MessageSpec(
        "action", "Проверка подключения AI Office завершилась ошибкой.", "ai-office", "ai-office"
    ),
    "financial_invariant_error": _MessageSpec(
        "blocking", "Финансовая операция заблокирована проверкой целостности.", form_or_modal="financial-action"
    ),
    "action_failed": _MessageSpec(
        "action", "Не удалось выполнить действие. Обновите страницу и повторите попытку.", form_or_modal="action"
    ),
}


_MESSAGE_ALIASES = {
    "Сначала включите 2FA в разделе Безопасность": "twofa_required",
    "Недостаточно прав": "permission_denied",
    "Недостаточно прав для создания этой роли": "permission_denied",
    "Not enough permissions": "permission_denied",
    "Email уже существует": "email_exists",
    "Новый пароль минимум 10 символов": "password_too_short",
    "Текущий пароль неверный": "current_password_incorrect",
    "Сначала создайте QR для 2FA": "twofa_prepare_required",
    "Неверный 2FA код": "twofa_invalid",
    "Для этой роли 2FA обязательна": "twofa_disable_forbidden",
    "Пользователь не найден": "user_not_found",
    "Этого пользователя нельзя изменить": "user_protected",
    "Этого пользователя нельзя заблокировать": "user_protected",
    "Мерчант не найден": "merchant_not_found",
    "Merchant not found.": "merchant_not_found",
    "Merchant не найден": "merchant_not_found",
    "Площадка не найдена": "platform_not_found",
    "Трейдер не найден": "trader_not_found",
    "Оператор не найден": "trader_not_found",
    "Реквизит не найден": "requisite_not_found",
    "Можно управлять только своими реквизитами": "requisite_scope_denied",
    "Можно редактировать только свои реквизиты": "requisite_scope_denied",
    "Удалять реквизиты может только оператор": "requisite_delete_denied",
    "Удалённый реквизит нельзя включить": "requisite_deleted",
    "Выберите трейдера для реквизита": "trader_required",
    "Unsupported payment method": "payment_method_invalid",
    "Choose a bank from the directory": "bank_required",
    "Choose a mobile operator from the directory": "mobile_operator_required",
    "Заявка не найдена": "deposit_not_found",
    "Webhook event not found.": "deposit_not_found",
    "Нет доступа к подтверждению этой заявки": "deposit_access_denied",
    "teamlead_assignment_conflict": "assignment_overlap",
    "teamlead_merchant_assignment_conflict": "assignment_overlap",
    "teamlead_rate_unavailable": "teamlead_rate_unavailable",
    "rolling_rate_unavailable": "rolling_rate_unavailable",
    "teamlead_settlement_conflict": "teamlead_settlement_conflict",
    "teamlead_settlement_tx_hash_duplicate": "teamlead_settlement_tx_hash_duplicate",
    "platform_wallet_concurrent_update": "platform_wallet_concurrent_update",
    "ai_office_m2m_auth_required": "ai_office_m2m_auth_required",
    "ai_office_credential_required": "ai_office_credential_required",
    "ai_office_config_not_found": "ai_office_config_not_found",
    "invalid_trc20_address": "trc20_invalid",
    "invalid_trc20_network": "trc20_invalid",
    "merchant_settlement_insufficient_available": "insufficient_balance",
    "teamlead_insufficient_funds": "insufficient_balance",
    "rolling_reconciliation_mismatch": "reconciliation_mismatch",
    "webhook_queue_unavailable": "webhook_queue_unavailable",
}


_SUCCESS_MESSAGES = {
    "Реквизит создан",
    "Статус реквизита изменен",
    "Реквизит обновлен",
    "Реквизит удален, но остается в истории для аудита",
    "Настройки интеграции сохранены",
    "Процент мерчанта сохранён",
    "Настройки трейдера сохранены",
    "QR код для 2FA создан",
    "2FA подключена",
    "2FA отключена",
    "Запрос settle отправлен superadmin",
    "Settle подтверждён, баланс мерчанта уменьшен",
    "Settle отклонён",
    "Настройки AI Office сохранены",
    "Credential удалён отдельным действием",
    "Настройки антискама сохранены",
    "Настройки трейдера сохранены",
    "Настройки реквизита сохранены",
    "Трейдер поставлен на паузу",
    "Реквизит поставлен на паузу",
    "Решение по трейдеру сохранено",
    "Решение по реквизиту сохранено",
    "Настройка вывода трейдера сохранена",
    "Назначение TeamLead сохранено с историей",
    "Назначение TeamLead мерчанту сохранено с историей",
    "Назначение TeamLead мерчанту закрыто",
    "Запрос TeamLead settle создан, средства заморожены",
    "TeamLead settle отклонён, frozen возвращён в available",
    "TeamLead settle выполнен, cooldown 168 часов запущен",
    "TeamLead accrual компенсирован",
    "Финансовая корректировка TeamLead записана в immutable ledger",
    "Комиссии сохранены",
    "Индивидуальный тариф создан",
    "Создана новая версия тарифа",
    "Тариф деактивирован",
    "Aggregator settings saved",
    "Aggregator status changed",
    "Апелляция создана",
    "Апелляция принята, баланс пересчитан",
    "Апелляция отправлена на решение admin/superadmin",
    "Срок обработки продлен",
    "Апелляция одобрена",
    "Апелляция отклонена",
}

_ALLOWED_SECTIONS = frozenset(
    {
        "create",
        "users",
        "merchants",
        "aggregators",
        "requisites",
        "traders",
        "security",
        "deposits",
        "appeals",
        "payouts",
        "antiscam",
        "wallet",
        "ai-office",
        "teamlead",
        "rolling",
        "webhook",
        "fees",
    }
)


def is_success_message(message: str | None) -> bool:
    return bool(message and message in _SUCCESS_MESSAGES)


def message_code(message: str | None) -> str:
    if not message:
        return "action_failed"
    if message in _SPECS:
        return message
    if message in _MESSAGE_ALIASES:
        return _MESSAGE_ALIASES[message]
    return "action_failed"


def resolve_ui_message(query_params) -> UiMessage | None:
    requested_code = str(query_params.get("ui_error") or "")
    legacy_message = str(query_params.get("msg") or "")
    code = requested_code if requested_code in _SPECS else ""
    if not code and legacy_message:
        candidate = _MESSAGE_ALIASES.get(legacy_message, "")
        code = candidate if candidate in _SPECS else ""
    if not code:
        return None
    spec = _SPECS[code]
    section_hint = str(query_params.get("section") or "").strip()
    resolved_section = section_hint if section_hint in _ALLOWED_SECTIONS else spec.section
    return UiMessage(
        kind=spec.kind,
        section=resolved_section,
        form_or_modal=spec.form_or_modal,
        field=spec.field,
        code=code,
        message=spec.message,
    )


def cabinet_redirect_url(
    base_path: str,
    *,
    message: str | None = None,
    section: str = "",
    success: bool = False,
    extra: dict[str, str] | None = None,
    fragment: str = "",
) -> str:
    params: dict[str, str] = {}
    if section:
        params["section"] = section
    if extra:
        params.update({key: value for key, value in extra.items() if value})
    if message and not success and not is_success_message(message):
        params["ui_error"] = message_code(message)
    query = "?" + urlencode(params) if params else ""
    anchor = "#" + fragment if fragment else ""
    return f"{base_path}{query}{anchor}"
