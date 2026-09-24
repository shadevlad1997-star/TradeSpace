from __future__ import annotations

from dataclasses import dataclass

from app.core.enums import PaymentMethod
from app.core.mobile_operators import find_mobile_operator, find_mobile_operator_by_code, get_mobile_operator_display_name
from app.core.payment_methods import is_card_method, is_mobile_commerce_method, is_sbp_method, normalize_payment_method
from app.core.russian_banks import find_bank, find_bank_by_code, get_bank_display_name


@dataclass(frozen=True)
class RequisiteProvider:
    provider_type: str
    bank_code: str | None
    operator_code: str | None
    display_name: str
    is_legacy: bool = False


def resolve_requisite_provider(
    method: str | None,
    *,
    bank_code: str | None = None,
    operator_code: str | None = None,
    bank_name: str | None = None,
    allow_legacy: bool = False,
) -> RequisiteProvider | None:
    normalized_method = normalize_payment_method(method, canonical_mobile=True)
    bank_code = (bank_code or '').strip() or None
    operator_code = (operator_code or '').strip() or None
    bank_name = (bank_name or '').strip() or None

    if normalized_method in {PaymentMethod.sbp.value, PaymentMethod.c2c.value}:
        bank = find_bank(bank_code or bank_name)
        if bank and bank.enabled:
            return RequisiteProvider('bank', bank.code, None, bank.display_name)
        if allow_legacy and bank_name:
            return RequisiteProvider('bank', None, None, bank_name, True)
        return None

    if normalized_method == PaymentMethod.mobile_commerce.value:
        operator = find_mobile_operator(operator_code or bank_name)
        if operator and operator.enabled:
            return RequisiteProvider('operator', None, operator.code, operator.display_name)
        if allow_legacy and bank_name:
            return RequisiteProvider('operator', None, None, bank_name, True)
        return None

    return None


def requisite_provider_type(method: str | None) -> str:
    if is_mobile_commerce_method(method):
        return 'operator'
    return 'bank'


def get_requisite_provider_type(requisite) -> str:
    return requisite_provider_type(getattr(requisite, 'method', None))


def requisite_provider_label(method: str | None) -> str:
    return 'Оператор' if is_mobile_commerce_method(method) else 'Банк'


def get_requisite_provider_label(requisite) -> str:
    return requisite_provider_label(getattr(requisite, 'method', None))


def requisite_provider_display(method: str | None, bank_code: str | None, operator_code: str | None, bank_name: str | None) -> str:
    if is_mobile_commerce_method(method):
        if operator_code:
            operator = find_mobile_operator_by_code(operator_code)
            if operator:
                return operator.display_name
        return get_mobile_operator_display_name(bank_name)

    if is_sbp_method(method) or is_card_method(method):
        if bank_code:
            bank = find_bank_by_code(bank_code)
            if bank:
                return bank.display_name
        return get_bank_display_name(bank_name)

    return (bank_name or '').strip()


def get_requisite_provider_display(requisite) -> str:
    return requisite_provider_display(
        getattr(requisite, 'method', None),
        getattr(requisite, 'bank_code', None),
        getattr(requisite, 'operator_code', None),
        getattr(requisite, 'bank_name', None),
    )


def requisite_provider_payload(method: str | None, bank_code: str | None, operator_code: str | None, bank_name: str | None) -> dict:
    normalized_method = normalize_payment_method(method, canonical_mobile=True) or method
    provider_type = requisite_provider_type(normalized_method)
    provider_name = requisite_provider_display(normalized_method, bank_code, operator_code, bank_name)
    payload = {
        'provider_type': provider_type,
        'provider_name': provider_name,
    }
    if provider_type == 'operator':
        payload.update({
            'operator_code': operator_code or '',
            'operator': provider_name,
            'bank_code': '',
            'bank': bank_name or '',
        })
    else:
        payload.update({
            'bank_code': bank_code or '',
            'bank': provider_name,
            'operator_code': '',
            'operator': '',
        })
    return payload
