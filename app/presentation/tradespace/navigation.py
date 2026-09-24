from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NavigationItem:
    slug: str
    label: str
    short_label: str
    description: str
    icon: str
    primary: bool = True


@dataclass(frozen=True, slots=True)
class RealmProfile:
    title: str
    eyebrow: str
    landing: str
    description: str
    navigation: tuple[NavigationItem, ...]


def _item(
    slug: str,
    label: str,
    short_label: str,
    description: str,
    icon: str,
    *,
    primary: bool = True,
) -> NavigationItem:
    return NavigationItem(slug, label, short_label, description, icon, primary)


TRADER_NAVIGATION = (
    _item("work", "Работа", "Работа", "Активная очередь и рабочее пространство операции.", "pulse"),
    _item("history", "История", "История", "Завершённые и недоступные для действия операции.", "history"),
    _item("requisites", "Реквизиты", "Реквизиты", "Собственные платёжные реквизиты и их доступность.", "card"),
    _item("finance", "Финансы", "Финансы", "Баланс, резерв, комиссии и финансовая история.", "wallet"),
    _item("disputes", "Споры", "Споры", "Апелляции по доступным операциям.", "dispute", primary=False),
    _item("analytics", "Аналитика", "Аналитика", "Операционная статистика трейдера.", "chart", primary=False),
    _item("notifications", "Уведомления", "События", "События, требующие внимания.", "bell", primary=False),
    _item("security", "Аккаунт", "Аккаунт", "Профиль, пароль, 2FA и параметры текущей сессии.", "shield", primary=False),
)

MERCHANT_NAVIGATION = (
    _item("overview", "Обзор", "Обзор", "Операции, баланс и доставка уведомлений.", "overview"),
    _item("operations", "Операции", "Операции", "Пополнения и выплаты мерчанта.", "pulse"),
    _item("finance", "Финансы", "Финансы", "Баланс, проводки и расчёты.", "wallet"),
    _item("settlements", "Расчёты", "Расчёты", "Запросы расчёта и их история.", "settlement"),
    _item("integration", "Интеграция", "API", "API, HMAC и доставка Webhook.", "integration"),
    _item("disputes", "Споры", "Споры", "Апелляции по операциям мерчанта.", "dispute", primary=False),
    _item("analytics", "Аналитика", "Аналитика", "Объём, конверсия и статусы.", "chart", primary=False),
    _item("security", "Аккаунт", "Аккаунт", "Профиль, пароль, 2FA и параметры текущей сессии.", "shield", primary=False),
)

TEAMLEAD_NAVIGATION = (
    _item("overview", "Обзор", "Обзор", "Состояние команды, начислений и расчётов.", "overview"),
    _item("team", "Команда", "Команда", "Назначенные трейдеры и мерчанты.", "team"),
    _item("accruals", "Начисления", "Начисления", "Реферальные начисления и история счёта.", "wallet"),
    _item("settlements", "Расчёты", "Расчёты", "Собственные запросы USDT TRC20.", "settlement"),
    _item("security", "Безопасность", "Профиль", "Обязательная 2FA, пароль и сессия.", "shield", primary=False),
)

STAFF_NAVIGATION = (
    _item("center", "Центр управления", "Центр", "Очереди, требующие внимания.", "overview"),
    _item("operations", "Операции", "Операции", "Пополнения, выплаты и обращения.", "pulse"),
    _item("network", "Сеть", "Сеть", "Участники и платёжные реквизиты.", "users"),
    _item("finance", "Финансы", "Финансы", "Балансы, начисления и расчёты.", "wallet"),
    _item("integrations", "Интеграции", "Интеграции", "API и доставка событий.", "integration"),
    _item("control", "Контроль", "Контроль", "Риски, доступ и журнал действий.", "shield"),
    _item("security", "Безопасность", "Аккаунт", "Пароль и обязательная 2FA.", "shield", primary=False),
)
SUPPORT_NAVIGATION = STAFF_NAVIGATION

AGGREGATOR_NAVIGATION = (
    _item("overview", "Обзор", "Обзор", "Состояние интеграции агрегатора.", "overview"),
    _item("payments", "Платежи", "Платежи", "Связанные платежи и статусы.", "pulse"),
    _item("callbacks", "Callbacks", "Callbacks", "Доставка уведомлений.", "integration"),
    _item("integration", "Интеграция", "API", "Настройка подключения.", "settings"),
    _item("security", "Безопасность", "Профиль", "Пароль и 2FA текущей учётной записи.", "shield", primary=False),
)

NOTIFICATIONS_ITEM = _item("notifications", "Уведомления", "События", "События, требующие внимания.", "bell", primary=False)

ROLE_PROFILES: dict[str, RealmProfile] = {
    "operator": RealmProfile("Рабочее место трейдера", "Трейдер", "work", "Очередь и контекст обработки платежей.", TRADER_NAVIGATION),
    "trader": RealmProfile("Рабочее место трейдера", "Трейдер", "work", "Очередь и контекст обработки платежей.", TRADER_NAVIGATION),
    "merchant": RealmProfile("Кабинет мерчанта", "Мерчант", "overview", "Операции, интеграция и расчёты.", MERCHANT_NAVIGATION),
    "teamlead": RealmProfile("Кабинет TeamLead", "TeamLead", "overview", "Команда, начисления и расчёты.", TEAMLEAD_NAVIGATION),
    "support": RealmProfile("Операционная поддержка", "Поддержка", "center", "Операции и обращения.", SUPPORT_NAVIGATION),
    "admin": RealmProfile("Центр управления", "Управление", "center", "Операции, участники и финансовая видимость.", STAFF_NAVIGATION),
    "superadmin": RealmProfile("Центр управления", "Управление", "center", "Управление платформой.", STAFF_NAVIGATION),
    "aggregator": RealmProfile("Кабинет агрегатора", "Агрегатор", "overview", "Платежи, callbacks и состояние интеграции.", AGGREGATOR_NAVIGATION),
}


def profile_for_role(role: str) -> RealmProfile:
    return ROLE_PROFILES[role]


def navigation_for_role(role: str) -> tuple[NavigationItem, ...]:
    navigation = profile_for_role(role).navigation
    if any(item.slug == "notifications" for item in navigation):
        return navigation
    return navigation + (NOTIFICATIONS_ITEM,)


MOBILE_DESTINATIONS: dict[str, tuple[str, ...]] = {
    "operator": ("work", "requisites", "finance", "disputes"),
    "trader": ("work", "requisites", "finance", "disputes"),
    "merchant": ("overview", "operations", "finance", "integration"),
    "teamlead": ("overview", "team", "accruals", "settlements"),
    "support": ("center", "operations", "network", "finance"),
    "admin": ("center", "operations", "network", "finance"),
    "superadmin": ("center", "operations", "network", "finance"),
    "aggregator": ("overview", "payments", "callbacks", "integration"),
}


def mobile_navigation_for_role(role: str) -> tuple[NavigationItem, ...]:
    items = {item.slug: item for item in navigation_for_role(role)}
    return tuple(items[slug] for slug in MOBILE_DESTINATIONS[role])


def mobile_overflow_for_role(role: str) -> tuple[NavigationItem, ...]:
    primary = set(MOBILE_DESTINATIONS[role])
    return tuple(item for item in navigation_for_role(role) if item.slug not in primary)


def section_for_role(role: str, slug: str) -> NavigationItem | None:
    return next((item for item in navigation_for_role(role) if item.slug == slug), None)



