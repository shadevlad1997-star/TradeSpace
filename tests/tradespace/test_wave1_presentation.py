import asyncio
import re
import uuid
from types import SimpleNamespace

import httpx
import pyotp
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.core.enums import Role
from app.core.security import encrypt_secret, hash_password
from app.db.session import engine as application_engine
from app.main import app
from app.models import AuditLog, Deposit, User
from app.web import routes as legacy_routes


OTP_SECRET = "JBSWY3DPEHPK3PXP"
PASSWORD = "tradespace-wave-one-password"
STAFF_ROLES = {"superadmin", "admin", "support", "teamlead"}
ROLE_REALM = {
    "superadmin": "staff",
    "admin": "staff",
    "support": "staff",
    "teamlead": "staff",
    "merchant": "merchant",
    "operator": "trader",
    "trader": "trader",
    "aggregator": "aggregator",
}


def _csrf(response: httpx.Response) -> str:
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', response.text)
    assert match, response.text[:1000]
    return match.group(1)


def _session_view(response: httpx.Response) -> str:
    match = re.search(r'name="session_view"\s+value="([^"]+)"', response.text)
    assert match, response.text[:1000]
    return match.group(1)


async def _make_users(*roles: str, staff_twofa: bool = True) -> list[User]:
    suffix = uuid.uuid4().hex
    users: list[User] = []
    async with AsyncSession(application_engine, expire_on_commit=False) as db:
        for index, role in enumerate(roles):
            uses_twofa = role in STAFF_ROLES and staff_twofa
            user = User(
                email=f"tradespace-{role}-{index}-{suffix}@example.test",
                password_hash=hash_password(PASSWORD),
                role=role,
                twofa_enabled=uses_twofa,
                twofa_secret=encrypt_secret(OTP_SECRET) if uses_twofa else None,
            )
            db.add(user)
            users.append(user)
        await db.commit()
    return users


async def _remove_users(users: list[User]) -> None:
    if not users:
        return
    async with AsyncSession(application_engine) as db:
        await db.execute(delete(User).where(User.id.in_([user.id for user in users])))
        await db.commit()


async def _login(client: httpx.AsyncClient, user: User, *, realm: str | None = None) -> httpx.Response:
    realm = realm or ROLE_REALM[user.role]
    page = await client.get(f"/{realm}/login")
    assert page.status_code == 200
    data = {"email": user.email, "password": PASSWORD, "csrf_token": _csrf(page)}
    if user.twofa_enabled:
        data["otp"] = pyotp.TOTP(OTP_SECRET).now()
    return await client.post(f"/{realm}/login", data=data, follow_redirects=False)


def test_wave1_router_adds_get_views_without_new_business_commands():
    presentation = [
        route
        for route in legacy_routes.router.routes
        if str(getattr(route, "name", "")).startswith("tradespace_")
    ]
    assert {route.name for route in presentation} == {
        "tradespace_cabinet",
        "tradespace_root",
        "tradespace_section",
        "tradespace_trader_operation",
        "tradespace_legacy_fallback",
        "tradespace_aggregator_secret",
    }
    assert all(set(route.methods or ()) <= {"GET", "HEAD"} for route in presentation)
    assert next(route for route in legacy_routes.router.routes if route.name == "change_password").methods == {"POST"}
    assert next(route for route in legacy_routes.router.routes if route.name == "enable_2fa").methods == {"POST"}


def test_wave1_http_auth_role_security_and_no_mutation_contracts():
    async def scenario():
        await application_engine.dispose(close=False)
        transport = httpx.ASGITransport(app=app)
        created: list[User] = []
        try:
            async with httpx.AsyncClient(transport=transport, base_url="https://localhost", headers={"X-TradeSpace-Preview": "wave1"}) as client:
                for realm in ("staff", "merchant", "trader", "aggregator"):
                    page = await client.get(f"/{realm}/login")
                    assert page.status_code == 200
                    assert "TradeSpace" in page.text
                    assert f'action="/{realm}/login"' in page.text
                    assert 'name="viewport"' in page.text
                    assert "TradeSpace" in page.text
                    assert _csrf(page)
                assert (await client.get("/teamlead/login")).status_code == 404

            role_users = await _make_users(*[role.value for role in Role])
            created.extend(role_users)
            for user in role_users:
                async with httpx.AsyncClient(transport=transport, base_url="https://localhost", headers={"X-TradeSpace-Preview": "wave1"}) as client:
                    login = await _login(client, user)
                    realm = ROLE_REALM[user.role]
                    assert login.status_code == 303
                    assert login.headers["location"] == f"/{realm}/cabinet"
                    cabinet = await client.get(login.headers["location"])
                    assert cabinet.status_code == 200
                    assert "TradeSpace" in cabinet.text
                    assert f'data-role="{user.role}"' in cabinet.text
                    assert 'data-component="RealmNavigation"' in cabinet.text
                    assert 'data-component="MobileNavigation"' in cabinet.text
                    assert f'action="/{realm}/logout"' in cabinet.text

            merchant = next(user for user in role_users if user.role == "merchant")
            trader = next(user for user in role_users if user.role == "trader")
            async with httpx.AsyncClient(transport=transport, base_url="https://localhost", headers={"X-TradeSpace-Preview": "wave1"}) as client:
                wrong = await _login(client, merchant, realm="staff")
                assert wrong.status_code == 403
                assert "другому кабинету" in wrong.text
            async with httpx.AsyncClient(transport=transport, base_url="https://localhost", headers={"X-TradeSpace-Preview": "wave1"}) as client:
                assert (await _login(client, trader)).status_code == 303
                forbidden = await client.get("/trader/cabinet/tradespace/settlements")
                assert forbidden.status_code == 403
                assert "Нет доступа" in forbidden.text
                assert "Этот раздел недоступен для вашей учётной записи." in forbidden.text

            async with httpx.AsyncClient(transport=transport, base_url="https://localhost", headers={"X-TradeSpace-Preview": "wave1"}) as client:
                unauthorized = await client.get("/merchant/cabinet", follow_redirects=False)
                assert unauthorized.status_code == 303
                assert unauthorized.headers["location"] == "/merchant/login"
                assert (await _login(client, merchant)).status_code == 303
                cabinet = await client.get("/merchant/cabinet")
                missing_csrf = await client.post(
                    "/merchant/cabinet/security/2fa/prepare",
                    follow_redirects=False,
                )
                assert missing_csrf.status_code == 403
                logout = await client.post(
                    "/merchant/logout",
                    data={"csrf_token": _csrf(cabinet), "session_view": _session_view(cabinet)},
                    follow_redirects=False,
                )
                assert logout.status_code == 303
                assert logout.headers["location"] == "/merchant/login"
                assert (await client.get("/merchant/cabinet", follow_redirects=False)).status_code == 303

            gated = await _make_users("teamlead", staff_twofa=False)
            created.extend(gated)
            async with httpx.AsyncClient(transport=transport, base_url="https://localhost", headers={"X-TradeSpace-Preview": "wave1"}) as client:
                login = await _login(client, gated[0])
                assert login.headers["location"] == "/staff/cabinet"
                cabinet = await client.get("/staff/cabinet")
                assert cabinet.status_code == 200
                assert "Для этой роли обязательна 2FA" in cabinet.text
                assert 'action="/staff/cabinet/security/2fa/prepare"' in cabinet.text
                assert 'data-role="teamlead"' in cabinet.text
                assert "TeamLead" in cabinet.text

            async with httpx.AsyncClient(transport=transport, base_url="https://localhost", headers={"X-TradeSpace-Preview": "wave1"}) as client:
                await _login(client, trader)
                async with AsyncSession(application_engine) as db:
                    before = (
                        await db.scalar(select(func.count(AuditLog.id))),
                        await db.scalar(select(func.count(Deposit.id))),
                    )
                for path in (
                    "/trader/cabinet",
                    "/trader/cabinet/tradespace/history",
                    "/trader/cabinet/tradespace/security",
                ):
                    assert (await client.get(path)).status_code == 200
                async with AsyncSession(application_engine) as db:
                    after = (
                        await db.scalar(select(func.count(AuditLog.id))),
                        await db.scalar(select(func.count(Deposit.id))),
                    )
                assert after == before

            replacement = await _make_users("merchant")
            created.extend(replacement)
            async with httpx.AsyncClient(transport=transport, base_url="https://localhost", headers={"X-TradeSpace-Preview": "wave1"}) as client:
                await _login(client, merchant)
                first_view = _session_view(await client.get("/merchant/cabinet"))
                await _login(client, replacement[0])
                changed = await client.get(
                    "/merchant/cabinet",
                    params={"session_view": first_view},
                )
                assert changed.status_code == 409
                assert "Сессия этой вкладки изменилась" in changed.text
                assert changed.headers["cache-control"] == "no-store, max-age=0"
        finally:
            await _remove_users(created)

    asyncio.run(scenario())


def test_wave1_one_time_secret_is_masked_and_not_cacheable():
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/cabinet/secret",
        "raw_path": b"/cabinet/secret",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("localhost", 443),
        "auth_realm": "staff",
        "csp_nonce": "test-nonce",
        "csrf_token": "test-csrf",
    }
    request = Request(scope)
    actor = SimpleNamespace(role="superadmin", email="staff@example.test")
    merchant = SimpleNamespace(id=uuid.uuid4(), name="Test merchant")
    secret = "one-time-secret-value"
    response = legacy_routes.merchant_credentials_response(
        request,
        actor,
        merchant,
        "merchant@example.test",
        "api-key-value",
        secret,
        "production",
    )
    body = response.body.decode("utf-8")
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert response.headers["pragma"] == "no-cache"
    assert secret in body
    assert 'id="secret-key"' in body
    assert "is-masked" in body
    assert "data-copy-target" in body
    assert "TradeSpace" in body






