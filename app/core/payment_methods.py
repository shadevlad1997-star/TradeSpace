from __future__ import annotations

from app.core.enums import PaymentMethod


PAYMENT_METHOD_LABELS: dict[str, str] = {
    PaymentMethod.sbp.value: 'СБП',
    PaymentMethod.c2c.value: 'Карта',
    PaymentMethod.mobile_commerce.value: 'Мобильная коммерция',
    PaymentMethod.mobile.value: 'Мобильная коммерция',
}

PAYMENT_METHOD_OPTIONS: tuple[dict[str, str], ...] = (
    {'value': PaymentMethod.sbp.value, 'label': PAYMENT_METHOD_LABELS[PaymentMethod.sbp.value]},
    {'value': PaymentMethod.c2c.value, 'label': PAYMENT_METHOD_LABELS[PaymentMethod.c2c.value]},
    {'value': PaymentMethod.mobile_commerce.value, 'label': PAYMENT_METHOD_LABELS[PaymentMethod.mobile_commerce.value]},
)

PAYMENT_METHOD_REQUISITE_PLACEHOLDERS: dict[str, str] = {
    PaymentMethod.sbp.value: 'Телефон СБП',
    PaymentMethod.c2c.value: 'Номер карты',
    PaymentMethod.mobile_commerce.value: 'Телефон для мобильной коммерции',
    PaymentMethod.mobile.value: 'Телефон для мобильной коммерции',
}

PAYMENT_METHOD_ALIASES: dict[str, str] = {
    'card': PaymentMethod.c2c.value,
    'card_number': PaymentMethod.c2c.value,
    'bank_transfer': PaymentMethod.c2c.value,
    'c2c': PaymentMethod.c2c.value,
    'sbp': PaymentMethod.sbp.value,
    'mobile': PaymentMethod.mobile.value,
    'mobile_commerce': PaymentMethod.mobile_commerce.value,
    'mobile-commerce': PaymentMethod.mobile_commerce.value,
}


def normalize_payment_method(value: str | None, *, canonical_mobile: bool = False) -> str | None:
    clean = (value or '').strip().lower().replace(' ', '_')
    method = PAYMENT_METHOD_ALIASES.get(clean)
    if method == PaymentMethod.mobile.value and canonical_mobile:
        return PaymentMethod.mobile_commerce.value
    return method


def payment_method_label(value: str | None) -> str:
    clean = (value or '').strip()
    return PAYMENT_METHOD_LABELS.get(clean, clean or '—')


def get_payment_method_display_name(value: str | None) -> str:
    return payment_method_label(normalize_payment_method(value, canonical_mobile=True) or value)


def is_sbp_method(value: str | None) -> bool:
    return normalize_payment_method(value, canonical_mobile=True) == PaymentMethod.sbp.value


def is_card_method(value: str | None) -> bool:
    return normalize_payment_method(value, canonical_mobile=True) == PaymentMethod.c2c.value


def is_mobile_commerce_method(value: str | None) -> bool:
    return normalize_payment_method(value, canonical_mobile=True) == PaymentMethod.mobile_commerce.value


def requisite_placeholder_for_method(value: str | None) -> str:
    clean = normalize_payment_method(value, canonical_mobile=True) or (value or '').strip()
    return PAYMENT_METHOD_REQUISITE_PLACEHOLDERS.get(clean, 'Телефон СБП / номер карты')
