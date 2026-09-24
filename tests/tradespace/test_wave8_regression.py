"""Presentation regression. PostgreSQL data is synthetic and rolled back.

HTTP/HTML checks are not a substitute for browser layout or touch verification.
"""
import asyncio
from html.parser import HTMLParser
from pathlib import Path
import re

import httpx
import pytest
from sqlalchemy import event, select, update

from app import models as m
from app.core.enums import DepositStatus, PayoutStatus, AppealStatus, RollingTransferStatus
from app.db.session import engine
from app.main import app
from app.presentation.tradespace.labels import reason_label
from app.presentation.tradespace.merchant.view_models import (
    DEPOSIT_STATUS, PAYOUT_STATUS, APPEAL_STATUS, ROLLING_STATUS,
)
from app.presentation.tradespace.navigation import navigation_for_role
from app.presentation.tradespace.staff.view_models import cell
from tests.tradespace.test_wave4_teamlead import login
from tests.tradespace.test_wave5_staff import fixture, client, CANARY

ICON_URL = "/static/tradespace/brand-mark.svg?v=8"


class TextContent(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def text_content(markup):
    parser = TextContent()
    parser.feed(markup)
    return " ".join(" ".join(parser.parts).split())


def assert_product_page(response):
    assert response.status_code == 200
    assert f'href="{ICON_URL}"' in response.text
    assert 'data-component="MobileNavigation"' in response.text
    assert 'data-component="AccountMenu"' in response.text
    assert "no-store" in response.headers["cache-control"]
    text = text_content(response.text)
    for phrase in (
        "Назначенные операции, деньги и состояния", "Foundation active",
        "следующая wave", "server permissions", "server command", "realm session",
        "workspace", "fee snapshot", "previous cabinet", "предыдущий кабинет",
        "business core", "golden", "freeze", "processing и деньги",
    ):
        assert phrase.lower() not in text.lower(), phrase
    assert CANARY not in response.text
    return text


@pytest.mark.parametrize("raw,label", [
    ("merchant_cancelled", "Отменено мерчантом"),
    ("trader_timeout", "Время подтверждения истекло"),
    ("future_machine_code", "Причина не уточнена"),
    ("Платёж не поступил: проверьте выписку", "Платёж не поступил: проверьте выписку"),
    (None, ""),
])
def test_reasons_are_display_only_and_keep_human_explanations(raw, label):
    assert reason_label(raw) == label
    assert cell("failure_reason", raw)["value"] == (label or "—")


@pytest.mark.parametrize("enum,labels", [
    (DepositStatus, DEPOSIT_STATUS), (PayoutStatus, PAYOUT_STATUS),
    (AppealStatus, APPEAL_STATUS), (RollingTransferStatus, ROLLING_STATUS),
])
def test_all_persisted_operation_states_have_human_labels(enum, labels):
    for member in enum:
        label, tone = labels[member.value]
        assert label != member.value and re.search("[А-Яа-я]", label)
        assert tone in {"neutral", "attention", "positive", "critical"}
    for key in ("status", "old_status", "new_status", "settlement_status"):
        assert cell(key, "future_machine_status")["value"] == "Статус не определён"


def test_favicon_is_explicit_on_login_and_matches_actual_root_asset():
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://localhost") as browser:
            for realm in ("trader", "merchant", "staff", "aggregator"):
                response = await browser.get(f"/{realm}/login")
                assert response.status_code == 200
                assert f'href="{ICON_URL}"' in response.text
                assert "realm" not in text_content(response.text).lower()
            explicit = await browser.get(ICON_URL)
            implicit = await browser.get("/favicon.ico")
            assert explicit.status_code == implicit.status_code == 200
            assert "image/svg+xml" in explicit.headers["content-type"]
            assert "image/svg+xml" in implicit.headers["content-type"]
            assert explicit.content == implicit.content == Path("app/static/tradespace/brand-mark.svg").read_bytes()
            assert b'aria-label="TradeSpace"' in explicit.content
            # Cache revalidation still resolves to the product asset.
            refreshed = await browser.get(ICON_URL, headers={"Cache-Control": "no-cache"})
            assert refreshed.content == explicit.content
            cached = await browser.get(ICON_URL, headers={"If-None-Match": explicit.headers["etag"]})
            assert cached.status_code == 304
    asyncio.run(scenario())


def test_six_role_navigation_and_text_are_product_only_without_get_writes():
    async def scenario():
        async with fixture() as (data, conn):
            # This fixture reuses its secret canary inside a public API-key identifier.
            # Give the public identifier a distinct value so the secret check is meaningful.
            await conn.execute(update(m.ApiKey).where(m.ApiKey.id == data["key"].id).values(api_key="public-ui-regression-key"))
            for key, realm in (("trader", "trader"), ("merchantowner", "merchant"),
                               ("lead", "staff"), ("support", "staff"),
                               ("admin", "staff"), ("super", "staff")):
                async with client() as browser:
                    user = data[key]
                    await login(browser, user, realm=realm)
                    writes = []

                    def observe(_a, _b, statement, *_):
                        if statement.lstrip().split(" ", 1)[0].upper() in {"UPDATE", "INSERT", "DELETE"}:
                            writes.append(statement)

                    event.listen(engine.sync_engine, "before_cursor_execute", observe)
                    try:
                        for item in navigation_for_role(user.role):
                            response = await browser.get(f"/{realm}/cabinet/tradespace/{item.slug}")
                            # Navigation input cannot bypass the staff section's server permissions.
                            if response.status_code == 403:
                                assert user.role == "support" and item.slug in {"security", "network", "control"}
                                continue
                            assert_product_page(response)
                            menu = re.search(r'<details class="ts-more-menu">.*?</details>', response.text, re.S).group()
                            assert "⌄" not in menu and '<svg class="ts-icon"' in menu
                            assert "<span>Ещё</span>" in menu
                            assert 'aria-expanded="false" aria-controls="mobile-more-sheet"' in response.text
                    finally:
                        event.remove(engine.sync_engine, "before_cursor_execute", observe)
                    assert not writes, (key, writes)
    asyncio.run(scenario())


def test_cancel_reason_is_localized_in_trader_merchant_staff_and_never_changes_money():
    async def scenario():
        async with fixture() as (data, conn):
            operation = data["pending"]
            metadata = {**operation.metadata_json, "failure_reason": "merchant_cancelled"}
            await conn.execute(update(m.Deposit).where(m.Deposit.id == operation.id).values(
                status="cancelled", metadata_json=metadata))
            trader_money = select(m.User.trader_balance, m.User.trader_hold).where(m.User.id == data["trader"].id)
            merchant_money = select(m.Balance.available, m.Balance.frozen).where(m.Balance.id == data["balance"].id)
            before = (tuple((await conn.execute(trader_money)).one()), tuple((await conn.execute(merchant_money)).one()))
            for key, realm, path in (
                ("trader", "trader", "/trader/cabinet/tradespace/history"),
                ("trader", "trader", f"/trader/cabinet/tradespace/operations/{operation.id}"),
                ("merchantowner", "merchant", f"/merchant/cabinet/tradespace/operations/{operation.id}"),
                ("support", "staff", f"/staff/cabinet/tradespace/operations/{operation.id}"),
                ("admin", "staff", f"/staff/cabinet/tradespace/operations/{operation.id}"),
                ("super", "staff", f"/staff/cabinet/tradespace/operations/{operation.id}"),
            ):
                async with client() as browser:
                    await login(browser, data[key], realm=realm)
                    response = await browser.get(path)
                    assert response.status_code == 200, (key, path)
                    text = text_content(response.text)
                    assert "Отменено мерчантом" in text
                    assert "merchant_cancelled" not in text
            after = (tuple((await conn.execute(trader_money)).one()), tuple((await conn.execute(merchant_money)).one()))
            assert after == before
            assert await conn.scalar(select(m.Deposit.status).where(m.Deposit.id == operation.id)) == "cancelled"
            assert await conn.scalar(select(m.Deposit.metadata_json).where(m.Deposit.id == operation.id)) == metadata
    asyncio.run(scenario())


def test_every_standalone_product_document_declares_the_shared_icon():
    for path in Path("app/templates/tradespace").rglob("*.html"):
        markup = path.read_text(encoding="utf-8")
        if "<head>" in markup:
            assert '{% include "tradespace/components/head_icons.html" %}' in markup, path
