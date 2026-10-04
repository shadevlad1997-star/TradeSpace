"""Strict server quote transport, polling metadata and failure throttling."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from starlette.requests import Request

from app.services import rapira, settlements


@pytest.fixture
def provider(monkeypatch):
    calls = []
    state = {"payload": {"data": [{"symbol": "USDT/RUB", "askPrice": "101.2345",
                                  "baseCurrency": "RUB", "quoteCurrency": "USDT",
                                  "bidPrice": "99", "close": "98"}]}}
    real_client = httpx.AsyncClient

    async def handler(request):
        calls.append(request)
        await asyncio.sleep(0)
        if state.get("error"):
            raise httpx.ReadTimeout("test timeout", request=request)
        return httpx.Response(state.get("status", 200), json=state["payload"])

    monkeypatch.setattr(rapira.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handler), **kwargs,
    ))
    monkeypatch.setattr(rapira.settings, "RAPIRA_RATES_ENABLED", True)
    monkeypatch.setattr(rapira.settings, "RAPIRA_RATES_URL", "https://provider.test/rates")
    monkeypatch.setattr(rapira.settings, "RAPIRA_RATES_CACHE_SECONDS", 15)
    monkeypatch.setattr(rapira.settings, "ROLLING_RAPIRA_MAX_AGE_SECONDS", 60)
    monkeypatch.setattr(rapira, "_strict_rolling_cache", None)
    monkeypatch.setattr(rapira, "_strict_request_loop", None)
    return state, calls


def test_concurrent_strict_reads_fetch_once_and_preserve_direct_ask(provider):
    _, calls = provider

    async def scenario():
        quotes = await asyncio.gather(*(rapira.get_strict_rolling_ask_quote() for _ in range(20)))
        assert all(q.rate == Decimal("101.2345") and q.side == "ask" for q in quotes)
        assert all(q.fetched_at == quotes[0].fetched_at for q in quotes)
        assert (await rapira.get_strict_rolling_ask_quote()).rate == quotes[0].rate

    asyncio.run(scenario())
    assert len(calls) == 1
    assert calls[0].headers["accept"] == "application/json"


@pytest.mark.parametrize("failure", ["http", "timeout", "pair", "missing_ask", "zero", "negative", "nan", "infinite", "stale"])
def test_unavailable_quotes_back_off_without_fallback_and_recover(provider, failure):
    state, calls = provider
    row = state["payload"]["data"][0]
    if failure == "http":
        state["status"] = 500
    elif failure == "timeout":
        state["error"] = True
    elif failure == "pair":
        row["symbol"] = "BTC/RUB"
    elif failure == "missing_ask":
        del row["askPrice"]
    elif failure == "stale":
        row["timestamp"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    else:
        row["askPrice"] = {"zero": "0", "negative": "-1", "nan": "NaN", "infinite": "Infinity"}[failure]

    async def scenario():
        results = await asyncio.gather(*(rapira.get_strict_rolling_ask_quote() for _ in range(20)), return_exceptions=True)
        assert all(isinstance(item, rapira.RollingRateUnavailable) for item in results)
        assert len(calls) == 1
        assert rapira._strict_rolling_cache is None
        # Simulate expiry of the cooldown; successful reads resume normally.
        rapira._strict_retry_after = 0
        state.update(error=False, status=200, payload={"data": [{"symbol": "USDT/RUB", "askPrice": "102.30"}]})
        assert (await rapira.get_strict_rolling_ask_quote()).rate == Decimal("102.30")
        assert len(calls) == 2

    asyncio.run(scenario())


class ReadOnlyDB:
    def __init__(self, result):
        self.result = result

    async def execute(self, _statement):
        return self

    def scalar_one_or_none(self):
        return self.result

    def scalar_one(self):
        return Decimal("0.00")


def test_quote_metadata_is_conservative_and_keeps_decimal_contract(monkeypatch):
    now = datetime.now(timezone.utc)
    quote = rapira.RollingRapiraQuote("USDT/RUB", Decimal("100.1234"), "ask", "rapira_live",
                                    now - timedelta(seconds=30), now, "provider_timestamp", False, "askPrice")

    async def current():
        return quote

    monkeypatch.setattr(settlements, "get_strict_rolling_ask_quote", current)
    monkeypatch.setattr(settlements.settings, "ROLLING_RAPIRA_MAX_AGE_SECONDS", 60)
    result = asyncio.run(settlements.merchant_settlement_quote(
        ReadOnlyDB(SimpleNamespace(available=Decimal("10000.00"))), uuid4(),
    ))
    assert result["rate_rub"] == Decimal("100.1234")
    assert result["rate_fetched_at"] == now
    assert result["rate_updated_at"] == quote.provider_timestamp
    assert 0 < result["rate_valid_for_seconds"] <= 30


@pytest.mark.parametrize("available", [True, False])
def test_merchant_rate_endpoint_uses_strict_quote_and_is_not_browser_cached(monkeypatch, available):
    from app.web import routes
    now = datetime.now(timezone.utc)
    merchant_id = uuid4()

    async def identity(_request):
        return False

    async def user(_request, _db):
        return SimpleNamespace(id=uuid4(), role="merchant", twofa_enabled=False)

    async def current(db, requested_id):
        assert requested_id == merchant_id
        if not available:
            raise settlements.MerchantSettlementRateUnavailable()
        return dict(rate_rub=Decimal("101.2345"), rate_source="rapira_live", rate_stale=False,
                    rate_updated_at=now, rate_fetched_at=now, rate_valid_for_seconds=59,
                    fee_usdt=Decimal("5.00"), fee_rub=Decimal("506.17"),
                    available_rub=Decimal("10000.00"), pending_rub=Decimal("0.00"),
                    max_request_usdt=Decimal("93.78"))

    monkeypatch.setattr(routes, "_session_identity_changed", identity)
    monkeypatch.setattr(routes, "get_current_web_user", user)
    monkeypatch.setattr(routes, "merchant_settlement_quote", current)
    request = Request({"type": "http", "method": "GET", "path": "/merchant/cabinet/rates/usdt-rub", "headers": []})
    response = asyncio.run(routes.cabinet_usdt_rub_rate(request, ReadOnlyDB(SimpleNamespace(id=merchant_id))))
    assert "no-store" in response.headers["cache-control"]
    payload = json.loads(response.body)
    if available:
        assert response.status_code == 200
        assert payload["rate_rub"] == "101.2345"
        assert payload["fetched_at"] == now.isoformat()
        assert payload["valid_for_seconds"] == 59
        assert payload["source"] == "rapira_live" and payload["stale"] is False
    else:
        assert response.status_code == 503
        assert "rate_rub" not in payload
        assert response.headers["retry-after"] == "20"
