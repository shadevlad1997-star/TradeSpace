"""Managed Russian bank directory for trader requisites.

The UI must show a compact, predictable list instead of a raw BIK registry.
Old database rows may still contain free-form bank text, so helper functions
keep read-side compatibility while new submissions can be validated strictly.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BankInfo:
    code: str
    display_name: str
    aliases: tuple[str, ...] = ()
    enabled: bool = True
    sort_order: int = 1000


TOP_RU_BANKS: tuple[BankInfo, ...] = (
    BankInfo('sber', 'Сбербанк', ('Сбер', 'Sber', 'СБЕР', 'Сбер Банк'), True, 10),
    BankInfo('tbank', 'Т-Банк', ('Тинькофф', 'Tinkoff', 'T-Bank', 'T Bank'), True, 20),
    BankInfo('vtb', 'ВТБ', ('VTB',), True, 30),
    BankInfo('alfabank', 'Альфа-Банк', ('Альфа', 'Alfa Bank', 'Альфа Банк'), True, 40),
    BankInfo('gazprombank', 'Газпромбанк', ('ГПБ', 'Gazprombank'), True, 50),
    BankInfo('raiffeisen', 'Райффайзен Банк', ('Райффайзен', 'Raiffeisen'), True, 60),
    BankInfo('rshb', 'Россельхозбанк', ('РСХБ', 'Rosselkhozbank'), True, 70),
    BankInfo('sovcombank', 'Совкомбанк', ('Sovcombank',), True, 80),
    BankInfo('mtsbank', 'МТС Банк', ('MTS Bank', 'МТС'), True, 90),
    BankInfo('pochtabank', 'Почта Банк', ('Pochta Bank',), True, 100),
    BankInfo('bspb', 'Банк Санкт-Петербург', ('БСПБ', 'Bank Saint Petersburg'), True, 110),
    BankInfo('uralsib', 'Уралсиб', ('Уралсиб Банк', 'Uralsib'), True, 120),
    BankInfo('rosbank', 'Росбанк', ('Rosbank',), True, 130),
    BankInfo('otpbank', 'ОТП Банк', ('OTP Bank',), True, 140),
    BankInfo('homebank', 'Хоум Банк', ('Хоум Кредит', 'Home Credit', 'Home Bank'), True, 150),
    BankInfo('rencredit', 'Ренессанс Банк', ('Ренессанс Кредит', 'Renaissance'), True, 160),
    BankInfo('akbars', 'Ак Барс Банк', ('Ак Барс', 'Ak Bars'), True, 170),
    BankInfo('psb', 'Промсвязьбанк', ('ПСБ', 'PSB'), True, 180),
    BankInfo('mkb', 'Московский Кредитный Банк', ('МКБ', 'MKB'), True, 190),
    BankInfo('openbank', 'Банк Открытие', ('Открытие', 'ФК Открытие', 'Otkritie'), True, 200),
    BankInfo('umoney', 'ЮMoney', ('ЮМани', 'YooMoney', 'ЮKassa', 'ЮКасса'), True, 210),
    BankInfo('ozonbank', 'Озон Банк', ('Ozon Bank', 'Ozon'), True, 220),
    BankInfo('yandexbank', 'Яндекс Банк', ('Yandex Bank', 'Яндекс Пэй'), True, 230),
    BankInfo('wbbank', 'Wildberries Банк', ('WB Банк', 'Вайлдберриз Банк', 'Wildberries'), True, 240),
    BankInfo('rsb', 'Русский Стандарт', ('Банк Русский Стандарт', 'Russian Standard'), True, 250),
    BankInfo('avangard', 'Авангард', ('Банк Авангард', 'Avangard'), True, 260),
    BankInfo('absolut', 'Абсолют Банк', ('Absolut Bank',), True, 270),
    BankInfo('sinara', 'Синара Банк', ('СКБ-Банк', 'Sinara'), True, 280),
    BankInfo('lokobank', 'Локо-Банк', ('Локо Банк', 'Loko Bank'), True, 290),
    BankInfo('expobank', 'Экспобанк', ('ExpoBank',), True, 300),
    BankInfo('unicredit', 'ЮниКредит Банк', ('Unicredit', 'Юникредит'), True, 310),
    BankInfo('zenit', 'Банк ЗЕНИТ', ('Зенит', 'Zenit'), True, 320),
    BankInfo('domrf', 'ДОМ.РФ Банк', ('Дом РФ', 'DOM.RF'), True, 330),
    BankInfo('bcsbank', 'БКС Банк', ('БКС', 'BCS Bank'), True, 340),
    BankInfo('crediteurope', 'Кредит Европа Банк', ('Credit Europe Bank',), True, 350),
    BankInfo('sdmbank', 'СДМ-Банк', ('СДМ', 'SDM Bank'), True, 360),
    BankInfo('metallinvestbank', 'Металлинвестбанк', ('Металлинвест', 'Metallinvestbank'), True, 370),
    BankInfo('ingobank', 'Ингосстрах Банк', ('Ingo Bank', 'Ингобанк'), True, 380),
    BankInfo('kubankredit', 'Кубань Кредит', ('Кубань Кредит Банк',), True, 390),
    BankInfo('centrinvest', 'Центр-инвест', ('Банк Центр-инвест', 'Центр Инвест'), True, 400),
    BankInfo('novikombank', 'Новикомбанк', ('Новиком', 'Novikombank'), True, 410),
    BankInfo('atb', 'Азиатско-Тихоокеанский Банк', ('АТБ', 'Asian-Pacific Bank'), True, 420),
    BankInfo('dvbank', 'Дальневосточный Банк', ('ДВБ', 'Dalnevostochny Bank'), True, 430),
    BankInfo('solidarnost', 'Солидарность Банк', ('Солидарность',), True, 440),
    BankInfo('genbank', 'Генбанк', ('Genbank',), True, 450),
    BankInfo('bystrobank', 'БыстроБанк', ('Быстро Банк', 'Bystrobank'), True, 460),
    BankInfo('modulbank', 'Модульбанк', ('Модуль Банк', 'Modulbank'), True, 470),
    BankInfo('tochka', 'Точка Банк', ('Точка', 'Tochka'), True, 480),
    BankInfo('forabank', 'Фора-Банк', ('Фора Банк', 'Fora Bank'), True, 490),
    BankInfo('rnkb', 'РНКБ', ('Российский Национальный Коммерческий Банк', 'RNKB'), True, 500),
    BankInfo('cifra_bank', 'Цифра банк', ('Цифра Банк', 'Cifra Bank'), True, 510),
    BankInfo(
        'mts_money_exi_bank',
        'МТС Деньги (ЭКСИ-Банк)',
        ('МТС Деньги', 'ЭКСИ-Банк', 'EXI Bank'),
        True,
        520,
    ),
)


def _norm(value: str | None) -> str:
    return (value or '').strip().casefold().replace('ё', 'е')


def get_enabled_banks() -> tuple[BankInfo, ...]:
    return tuple(sorted((bank for bank in TOP_RU_BANKS if bank.enabled), key=lambda item: item.sort_order))


def find_bank_by_code(code: str | None) -> BankInfo | None:
    normalized = _norm(code)
    return next((bank for bank in TOP_RU_BANKS if _norm(bank.code) == normalized), None)


def find_bank(value: str | None) -> BankInfo | None:
    normalized = _norm(value)
    if not normalized:
        return None
    for bank in TOP_RU_BANKS:
        if normalized in {_norm(bank.code), _norm(bank.display_name)}:
            return bank
        if any(_norm(alias) == normalized for alias in bank.aliases):
            return bank
    return None


def normalize_bank_name(value: str | None, *, allow_legacy: bool = False) -> str | None:
    clean = (value or '').strip()
    if not clean:
        return None
    bank = find_bank(clean)
    if bank and bank.enabled:
        return bank.display_name
    return clean if allow_legacy else None


def get_bank_display_name(value: str | None) -> str:
    bank = find_bank(value)
    return bank.display_name if bank else (value or '').strip()


def is_legacy_bank(value: str | None) -> bool:
    clean = (value or '').strip()
    return bool(clean and not find_bank(clean))


def search_banks(query: str | None, *, limit: int = 50) -> tuple[BankInfo, ...]:
    normalized = _norm(query)
    banks = get_enabled_banks()
    if not normalized:
        return banks[:limit]
    matches = [
        bank for bank in banks
        if normalized in _norm(bank.display_name)
        or normalized in _norm(bank.code)
        or any(normalized in _norm(alias) for alias in bank.aliases)
    ]
    return tuple(matches[:limit])


RF_BANKS: tuple[str, ...] = tuple(bank.display_name for bank in get_enabled_banks())

