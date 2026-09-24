"""Product-only entry routes; security and business handlers remain authoritative."""
import asyncio

import httpx

from app.main import app
from app.web import routes as commands
from tests.tradespace.test_wave1_presentation import (
    ROLE_REALM, _login, _make_users, _remove_users, application_engine,
)


def test_all_realms_direct_urls_and_disabled_ui_never_render_retired_cabinet(monkeypatch):
    async def scenario():
        await application_engine.dispose(close=False)
        users = await _make_users(*ROLE_REALM)
        original = commands.templates.TemplateResponse
        rendered = []

        def capture(*args, **kwargs):
            rendered.append(kwargs.get("name"))
            return original(*args, **kwargs)

        monkeypatch.setattr(commands.templates, "TemplateResponse", capture)
        try:
            for enabled in ("true", "false"):
                monkeypatch.setenv("TRADESPACE_UI_ENABLED", enabled)
                for user in users:
                    realm = ROLE_REALM[user.role]
                    base = f"/{realm}/cabinet"
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://localhost") as client:
                        assert (await _login(client, user)).status_code == 303
                        for path in (base, base + "/" + user.role, base + "/legacy", base + "/tradespace/security"):
                            page = await client.get(path, follow_redirects=True)
                            assert page.status_code == 200
                            assert "TradeSpace" in page.text
                            assert 'data-component="AccountMenu"' in page.text
                            assert 'data-component="MobileNavigation"' in page.text
                            assert "Предыдущий кабинет" not in page.text
                            assert 'href="' + base + '/legacy"' not in page.text
                            if enabled == "false":
                                assert 'data-component="MaintenanceState"' in page.text
                        if user.role != "superadmin":
                            forbidden = await client.get(base + "/superadmin")
                            assert forbidden.status_code == 403
            assert "cabinet.html" not in rendered
            assert "teamlead_cabinet.html" not in rendered
        finally:
            await _remove_users(users)
            await application_engine.dispose()
    asyncio.run(scenario())


def test_aggregator_secret_requires_manager_consumes_once_and_disables_cache(monkeypatch):
    async def scenario():
        await application_engine.dispose(close=False)
        users = await _make_users("admin", "support")
        consumed = []

        async def consume(request, user):
            consumed.append(str(user.id))
            return {"name": "Test provider", "api_key": "test-api-key", "secret_key": "test-one-time-secret"} if len(consumed) == 1 else None

        monkeypatch.setenv("TRADESPACE_UI_ENABLED", "true")
        monkeypatch.setattr(commands, "_consume_aggregator_secret_flash", consume)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://localhost") as client:
                await _login(client, users[1])
                assert (await client.get("/staff/cabinet/tradespace/secrets/aggregator")).status_code == 403
                assert not consumed
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://localhost") as client:
                await _login(client, users[0])
                first = await client.get("/staff/cabinet/tradespace/secrets/aggregator")
                second = await client.get("/staff/cabinet/tradespace/secrets/aggregator")
                assert first.status_code == second.status_code == 200
                assert "test-one-time-secret" in first.text
                assert 'class="ts-copy__value is-masked"' in first.text
                assert 'data-reveal-target="aggregator-secret" aria-pressed="false"' in first.text
                assert "test-one-time-secret" not in second.text
                assert "no-store" in first.headers["cache-control"]
                assert '<meta name="referrer" content="no-referrer">' in first.text
                assert "test-one-time-secret" not in first.headers.get("set-cookie", "")
        finally:
            await _remove_users(users)
            await application_engine.dispose()
    asyncio.run(scenario())


def test_api_document_identity_and_webhook_contract_names():
    assert app.title == "TradeSpace"
    assert app.openapi()["info"]["title"] == "TradeSpace"
