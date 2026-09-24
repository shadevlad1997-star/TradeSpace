from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.enums import PaymentMethod
from app.core.requisite_providers import (
    requisite_provider_display,
    requisite_provider_payload,
    resolve_requisite_provider,
)
from app.core.russian_banks import (
    find_bank,
    find_bank_by_code,
    get_enabled_banks,
    search_banks,
)
from app.schemas.common import RequisiteIn
from app.services.requisites import SelectedPaymentRequisite


NEW_BANKS = (
    ('cifra_bank', 'Цифра банк'),
    ('mts_money_exi_bank', 'МТС Деньги (ЭКСИ-Банк)'),
)


@pytest.mark.parametrize(('code', 'name'), NEW_BANKS)
def test_new_banks_are_enabled_and_searchable(code: str, name: str):
    by_code = find_bank_by_code(code)
    assert by_code is not None
    assert by_code.code == code
    assert by_code.display_name == name
    assert by_code.enabled is True
    assert find_bank(name) == by_code
    assert by_code in search_banks(name)
    assert by_code in get_enabled_banks()


def test_mts_money_exi_bank_is_distinct_from_mts_bank():
    mts_bank = find_bank_by_code('mtsbank')
    mts_money = find_bank_by_code('mts_money_exi_bank')

    assert mts_bank is not None
    assert mts_money is not None
    assert mts_bank.code == 'mtsbank'
    assert mts_bank.display_name == 'МТС Банк'
    assert mts_money.code == 'mts_money_exi_bank'
    assert mts_money.display_name == 'МТС Деньги (ЭКСИ-Банк)'
    assert mts_bank != mts_money
    assert find_bank('МТС') == mts_bank
    assert find_bank('МТС Деньги') == mts_money


def test_bank_directory_identity_fields_remain_unique():
    banks = get_enabled_banks()
    assert len({bank.code for bank in banks}) == len(banks)
    assert len({bank.display_name for bank in banks}) == len(banks)
    assert len({bank.sort_order for bank in banks}) == len(banks)


@pytest.mark.parametrize(('code', 'name'), NEW_BANKS)
@pytest.mark.parametrize('method', (PaymentMethod.sbp, PaymentMethod.c2c))
def test_requisite_schema_normalizes_new_bank_codes(
    code: str,
    name: str,
    method: PaymentMethod,
):
    requisite = RequisiteIn(
        owner_name='Test trader',
        method=method,
        value='+79990000000' if method == PaymentMethod.sbp else '4111111111111111',
        bank_code=code,
    )

    assert requisite.bank_code == code
    assert requisite.bank_name == name
    assert requisite.operator_code is None


@pytest.mark.parametrize(('code', 'name'), NEW_BANKS)
def test_requisite_provider_serializes_new_bank(code: str, name: str):
    provider = resolve_requisite_provider('sbp', bank_code=code)
    assert provider is not None
    assert provider.bank_code == code
    assert provider.display_name == name

    assert requisite_provider_display('sbp', code, None, None) == name
    assert requisite_provider_payload('sbp', code, None, None) == {
        'provider_type': 'bank',
        'provider_name': name,
        'bank_code': code,
        'bank': name,
        'operator_code': '',
        'operator': '',
    }

    selected = SelectedPaymentRequisite(
        requisite=SimpleNamespace(
            method='sbp',
            bank_code=code,
            operator_code=None,
            bank_name=name,
            full_name='Test trader',
            owner_name='Test trader',
        ),
        value='+79990000000',
        exact_amount=Decimal('1000.00'),
    )
    payment_details = selected.payment_details()
    assert payment_details['bank_code'] == code
    assert payment_details['bank_name'] == name
    assert payment_details['bank'] == name


def test_unknown_bank_code_remains_rejected():
    with pytest.raises(ValidationError, match='bank_code is required'):
        RequisiteIn(
            owner_name='Test trader',
            method=PaymentMethod.sbp,
            value='+79990000000',
            bank_code='not_in_bank_directory',
        )
