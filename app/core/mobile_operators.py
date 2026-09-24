"""Managed Russian mobile operator directory for mobile commerce requisites."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MobileOperatorInfo:
    code: str
    display_name: str
    aliases: tuple[str, ...] = ()
    enabled: bool = True
    sort_order: int = 1000


MOBILE_OPERATORS: tuple[MobileOperatorInfo, ...] = (
    MobileOperatorInfo('mts', 'МТС', ('MTS',), True, 10),
    MobileOperatorInfo('megafon', 'МегаФон', ('Megafon', 'Мегафон'), True, 20),
    MobileOperatorInfo('beeline', 'Билайн', ('Beeline', 'БиЛайн'), True, 30),
    MobileOperatorInfo('t2', 'T2', ('Tele2', 'Теле2', 'Т2'), True, 40),
    MobileOperatorInfo('yota', 'Yota', ('Йота',), True, 50),
    MobileOperatorInfo('rostelecom', 'Ростелеком', ('Rostelecom',), True, 60),
    MobileOperatorInfo('sbermobile', 'СберМобайл', ('Сбер Мобайл', 'SberMobile'), True, 70),
    MobileOperatorInfo('tmobile', 'Т-Мобайл', ('Т Мобайл', 'T-Mobile', 'T Mobile'), True, 80),
    MobileOperatorInfo('alfamobile', 'Альфа-Мобайл', ('Альфа Мобайл', 'Alfa Mobile'), True, 90),
    MobileOperatorInfo('gazprommobile', 'Газпром Мобайл', ('Газпром-Мобайл', 'Gazprom Mobile'), True, 100),
)


def _norm(value: str | None) -> str:
    return (value or '').strip().casefold().replace('ё', 'е')


def get_enabled_mobile_operators() -> tuple[MobileOperatorInfo, ...]:
    return tuple(sorted((operator for operator in MOBILE_OPERATORS if operator.enabled), key=lambda item: item.sort_order))


def find_mobile_operator_by_code(value: str | None) -> MobileOperatorInfo | None:
    normalized = _norm(value)
    return next((operator for operator in MOBILE_OPERATORS if _norm(operator.code) == normalized), None)


def find_mobile_operator(value: str | None) -> MobileOperatorInfo | None:
    normalized = _norm(value)
    if not normalized:
        return None
    for operator in MOBILE_OPERATORS:
        if normalized in {_norm(operator.code), _norm(operator.display_name)}:
            return operator
        if any(_norm(alias) == normalized for alias in operator.aliases):
            return operator
    return None


def normalize_mobile_operator(value: str | None, *, allow_legacy: bool = False) -> str | None:
    clean = (value or '').strip()
    if not clean:
        return None
    operator = find_mobile_operator(clean)
    if operator and operator.enabled:
        return operator.code
    return clean if allow_legacy else None


def get_mobile_operator_display_name(value: str | None) -> str:
    operator = find_mobile_operator(value)
    return operator.display_name if operator else (value or '').strip()


def is_legacy_mobile_operator(value: str | None) -> bool:
    clean = (value or '').strip()
    return bool(clean and not find_mobile_operator(clean))


def search_mobile_operators(query: str | None, *, limit: int = 10) -> tuple[MobileOperatorInfo, ...]:
    normalized = _norm(query)
    operators = get_enabled_mobile_operators()
    if not normalized:
        return operators[:limit]
    matches = [
        operator for operator in operators
        if normalized in _norm(operator.display_name)
        or normalized in _norm(operator.code)
        or any(normalized in _norm(alias) for alias in operator.aliases)
    ]
    return tuple(matches[:limit])
