import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.rapira import parse_strict_rolling_ask_quote
from rehearsal.rapira_mock import MOCK_RATE, SCENARIOS, scenario_response


NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def _payload(scenario: str) -> dict:
    response = scenario_response(scenario, now=NOW)
    assert response.content_type == "application/json"
    return json.loads(response.body)


def test_mock_contract_success_uses_canonical_live_ask():
    quote = parse_strict_rolling_ask_quote(
        _payload("success"),
        fetched_at=NOW,
        evaluated_at=NOW,
    )

    assert quote is not None
    assert quote.rate == Decimal(MOCK_RATE)
    assert quote.symbol == "USDT/RUB"
    assert quote.side == "ask"
    assert quote.source == "rapira_live"
    assert quote.provider_timestamp == NOW
    assert quote.fetched_at == NOW
    assert quote.freshness_basis == "provider_timestamp"
    assert quote.provider_field == "askPrice"


def test_mock_contract_without_timestamp_uses_fetched_at():
    quote = parse_strict_rolling_ask_quote(
        _payload("success_without_provider_timestamp"),
        fetched_at=NOW,
        evaluated_at=NOW,
    )

    assert quote is not None
    assert quote.rate == Decimal(MOCK_RATE)
    assert quote.provider_timestamp is None
    assert quote.fetched_at == NOW
    assert quote.freshness_basis == "fetched_at"


@pytest.mark.parametrize(
    "scenario",
    [
        "missing_ask_price",
        "invalid_symbol",
        "stale_response",
    ],
)
def test_mock_unavailable_payloads_do_not_return_strict_quote(scenario):
    assert parse_strict_rolling_ask_quote(
        _payload(scenario),
        fetched_at=NOW,
        evaluated_at=NOW,
    ) is None


def test_mock_malformed_json_is_not_parseable():
    response = scenario_response("malformed_json", now=NOW)

    with pytest.raises(json.JSONDecodeError):
        json.loads(response.body)


def test_mock_http_500_and_timeout_are_stable():
    server_error = scenario_response("http_500", now=NOW)
    timeout = scenario_response("timeout", now=NOW)

    assert server_error.status == 500
    assert timeout.status == 200
    assert timeout.delay_seconds > 5


def test_mock_metadata_conflict_uses_symbol_and_safe_warning(caplog):
    payload = _payload("metadata_conflict")
    with caplog.at_level(logging.WARNING, logger="app.finance"):
        quote = parse_strict_rolling_ask_quote(
            payload,
            fetched_at=NOW,
            evaluated_at=NOW,
        )

    assert quote is not None
    assert quote.symbol == "USDT/RUB"
    assert quote.rate == Decimal(MOCK_RATE)
    warning = next(
        record
        for record in caplog.records
        if record.getMessage() == "rolling_rapira_pair_metadata_conflict"
    )
    assert warning.symbol == "USDT/RUB"
    assert warning.base_currency == "RUB"
    assert warning.quote_currency == "USDT"
    assert MOCK_RATE not in warning.getMessage()
    assert "askPrice" not in warning.getMessage()


def test_mock_declares_exactly_the_required_scenarios():
    assert SCENARIOS == {
        "success",
        "success_without_provider_timestamp",
        "malformed_json",
        "missing_ask_price",
        "invalid_symbol",
        "http_500",
        "timeout",
        "stale_response",
        "metadata_conflict",
    }


def test_mock_compose_is_internal_only_and_hardened():
    compose = (
        ROOT / "rehearsal/compose.rapira-mock.yml"
    ).read_text(encoding="utf-8")

    assert "\n    ports:" not in compose
    assert '\n      - "8080"' in compose
    assert "read_only: true" in compose
    assert "no-new-privileges:true" in compose
    assert "cap_drop:" in compose
    assert "\n      - ALL" in compose
    assert "\n      - backend" in compose
    assert compose.count("image: ${REHEARSAL_APP_IMAGE:") == 2
