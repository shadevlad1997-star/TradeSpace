"""TeamLead presentation contracts; every fixture is rolled back, including command commits."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import re
from types import SimpleNamespace
import uuid

import httpx
import pyotp
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import encrypt_secret, hash_password
from app.presentation.tradespace.teamlead.formatting import teamlead_usdt
from app.presentation.tradespace.teamlead import estimate as estimate_view
from app.db.session import engine, get_db
from app.main import app
from app.models import (
    Deposit, Merchant, TeamLeadBalance, TeamLeadLedgerEntry, TeamLeadSettlement, User,
)
from app.services.rapira import RollingRapiraQuote
from app.services.teamlead import (
    accrue_teamlead_commission, accrue_teamlead_merchant_commission,
    adjust_teamlead_balance, create_or_replace_assignment,
    create_or_replace_merchant_assignment, create_teamlead_settlement,
    complete_teamlead_settlement, reject_teamlead_settlement, reverse_teamlead_accrual,
)
from app.web import routes as legacy

PASSWORD = "tradespace-wave-four-local-test"
OTP = "JBSWY3DPEHPK3PXP"
WALLET = "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb"
BASE = "/staff/cabinet"
SECTIONS = ("overview", "team", "accruals", "settlements", "security")


def csrf(response):
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def quote(at=None):
    return RollingRapiraQuote(
        symbol="USDT/RUB", rate=Decimal("100"), side="ask", source="rapira_live",
        provider_timestamp=None, fetched_at=at or datetime.now(timezone.utc),
        freshness_basis="fetched_at", stale=False, provider_field="askPrice",
    )


def test_teamlead_usdt_is_display_only_and_two_decimals():
    for stored, display in (("5.000000", "5.00"), ("1.000000", "1.00"),
                            ("0.500000", "0.50"), ("0.000000", "0.00")):
        original = Decimal(stored)
        assert teamlead_usdt(original) == display
        assert str(original) == stored


def test_teamlead_estimate_uses_separately_rounded_rub_costs():
    rate = Decimal("100")
    for balance, expected in (("0", "0.00"), ("499.99", "0.00"),
                              ("500.00", "0.00"), ("500.99", "0.00"),
                              ("501.00", "0.01"), ("550.00", "0.50"),
                              ("600.00", "1.00")):
        assert estimate_view.affordable_usdt(Decimal(balance), rate) == Decimal(expected)
    # Fee = 5.03 RUB and a 1.00 USDT request = 1.01 RUB after separate rounding.
    assert estimate_view.affordable_usdt(Decimal("6.03"), Decimal("1.005")) == Decimal("0.99")


def test_teamlead_estimate_endpoint_is_scoped_fresh_and_read_only(monkeypatch):
    async def fresh_quote():
        return quote()

    monkeypatch.setattr(estimate_view, "get_strict_rolling_ask_quote", fresh_quote)

    async def scenario():
        async with fixture() as (data, conn):
            endpoint = f"{BASE}/tradespace/teamlead-estimate"
            async with client() as anonymous:
                assert (await anonymous.get(endpoint)).status_code == 401
            async with client() as c:
                await login(c, data["lead"])
                writes = []
                def observe(_conn, _cursor, statement, parameters, context, executemany):
                    if statement.lstrip().split(" ", 1)[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
                        writes.append(statement)
                event.listen(engine.sync_engine, "before_cursor_execute", observe)
                try:
                    result = await c.get(endpoint)
                    assert result.status_code == 200, result.text
                    assert result.headers["cache-control"].startswith("no-store")
                    assert result.json()["source"] == "rapira_live"
                    assert re.fullmatch(r"\d+\.\d{2}", result.json()["available_usdt"])
                    assert result.json()["valid_for_seconds"] > 0
                    overview = await c.get(BASE)
                    settlements = await c.get(f"{BASE}/tradespace/settlements")
                    for page in (overview, settlements):
                        assert f'data-estimate-url="{endpoint}"' in page.text
                        assert "USDT: курс недоступен" in page.text
                    assert "<span>Мерчанты:</span> <strong>1</strong>" in overview.text
                    assert "<span>Трейдеры:</span> <strong>1</strong>" in overview.text
                    assert "с незакрытым назначением" not in overview.text
                    assert not writes, writes
                finally:
                    event.remove(engine.sync_engine, "before_cursor_execute", observe)
                async def old_quote():
                    return quote(datetime.now(timezone.utc) - timedelta(seconds=120))
                monkeypatch.setattr(estimate_view, "get_strict_rolling_ask_quote", old_quote)
                unavailable = await c.get(endpoint)
                assert unavailable.status_code == 503
                assert "available_usdt" not in unavailable.text
            monkeypatch.setattr(estimate_view, "get_strict_rolling_ask_quote", fresh_quote)
            async with client() as empty:
                await login(empty, data["empty"])
                assert (await empty.get(endpoint)).json()["available_usdt"] == "0.00"
                assert not await conn.scalar(select(TeamLeadBalance.id).where(
                    TeamLeadBalance.teamlead_id == data["empty"].id))
            for key in ("gated", "support", "admin", "super"):
                async with client() as denied:
                    await login(denied, data[key])
                    assert (await denied.get(endpoint)).status_code == 403
            for key, realm in (("merchantowner", "merchant"), ("trader", "trader")):
                async with client() as denied:
                    await login(denied, data[key], realm=realm)
                    assert (await denied.get(
                        f"/{realm}/cabinet/tradespace/teamlead-estimate"
                    )).status_code == 403
    asyncio.run(scenario())


async def seed(db):
    now = datetime.now(timezone.utc)
    tag = uuid.uuid4().hex[:10]
    users = {}
    for key, role in (("lead", "teamlead"), ("foreign", "teamlead"), ("empty", "teamlead"),
                      ("gated", "teamlead"), ("admin", "admin"), ("support", "support"),
                      ("super", "superadmin"), ("trader", "trader"), ("othertrader", "trader"),
                      ("merchantowner", "merchant")):
        twofa = role in {"teamlead", "admin", "superadmin", "support"} and key != "gated"
        users[key] = User(email=f"wave4-{key}-{tag}@example.test", role=role,
            password_hash=hash_password(PASSWORD), twofa_enabled=twofa,
            twofa_secret=encrypt_secret(OTP) if twofa else None)
        db.add(users[key])
    await db.flush()
    merchant = Merchant(owner_id=users["merchantowner"].id,
        name="Тестовый мерчант с длинным названием для проверки переноса " + tag)
    db.add(merchant)
    await db.flush()
    for key, trader in (("lead", "trader"), ("foreign", "othertrader")):
        await create_or_replace_assignment(db, teamlead_id=users[key].id,
            trader_id=users[trader].id, commission_percent=Decimal("0.75"),
            actor_id=users["super"].id, reason="Wave 4 rollback-only fixture",
            effective_from=now-timedelta(days=15))
        await adjust_teamlead_balance(db, teamlead_id=users[key].id,
            adjustment_type="available_credit", amount_rub=Decimal("12345678.90"),
            actor_id=users["super"].id, reason="Wave 4 synthetic account",
            idempotency_key=f"wave4-credit-{key}-{tag}")
    await create_or_replace_merchant_assignment(db, teamlead_id=users["lead"].id,
        merchant_id=merchant.id, commission_percent=Decimal("0.25"),
        actor_id=users["super"].id, reason="Wave 4 merchant referral",
        valid_from=now-timedelta(days=15))
    await adjust_teamlead_balance(db, teamlead_id=users["lead"].id,
        adjustment_type="debt_increase", amount_rub=Decimal("100"),
        actor_id=users["super"].id, reason="Wave 4 debt-first fixture",
        idempotency_key=f"wave4-debt-{tag}")
    deposits = []
    for index, trader in enumerate(("trader", "trader", "othertrader")):
        deposit = Deposit(merchant_id=merchant.id, external_id=f"wave4-source-{tag}-{index}",
            amount=Decimal("100000"), currency="RUB", method="sbp", status="paid",
            created_at=now-timedelta(days=12-index), expires_at=now, metadata_json={"secret": "DO-NOT-EXPOSE"})
        db.add(deposit)
        await db.flush()
        snapshot = SimpleNamespace(executor_type="trader", executor_id=users[trader].id,
            calculation_base_amount=deposit.amount)
        await accrue_teamlead_commission(db, deposit=deposit, snapshot=snapshot,
            platform_income_rub=Decimal("5000"))
        if index == 0:
            await accrue_teamlead_merchant_commission(db, deposit=deposit, snapshot=snapshot,
                platform_income_rub=Decimal("5000"))
        if index == 1:
            await reverse_teamlead_accrual(db, deposit_id=deposit.id,
                actor_id=users["super"].id, reason="Тестовая отмена начисления")
        deposits.append(deposit)
    completed_at = now-timedelta(days=10)
    completed = await create_teamlead_settlement(db, teamlead_id=users["lead"].id,
        requested_usdt=Decimal("10"), wallet_address=WALLET, quote=quote(completed_at),
        idempotency_key=f"wave4-completed-{tag}", now=completed_at)
    await complete_teamlead_settlement(db, settlement_id=completed.id,
        actor_id=users["super"].id, tx_hash=f"wave4-local-evidence-{tag}", now=completed_at)
    rejected = await create_teamlead_settlement(db, teamlead_id=users["lead"].id,
        requested_usdt=Decimal("2.123456"), wallet_address=WALLET, quote=quote(),
        idempotency_key=f"wave4-rejected-{tag}")
    await reject_teamlead_settlement(db, settlement_id=rejected.id,
        actor_id=users["super"].id, reason="Тестовое отклонение: проверьте адрес")
    await db.flush()
    return {**users, "merchant": merchant, "deposits": deposits, "completed": completed}


@asynccontextmanager
async def fixture():
    await engine.dispose(close=False)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False,
                                join_transaction_mode="create_savepoint") as db:
            data = await seed(db)
            await db.commit()
        async def session_override():
            async with AsyncSession(bind=connection, expire_on_commit=False,
                                    join_transaction_mode="create_savepoint") as session:
                yield session
        original = app.dependency_overrides.get(get_db)
        app.dependency_overrides[get_db] = session_override
        try:
            yield data, connection
        finally:
            if original is None:
                app.dependency_overrides.pop(get_db, None)
            else:
                app.dependency_overrides[get_db] = original
            await transaction.rollback()


def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
        base_url="https://localhost", headers={"X-TradeSpace-Preview": "wave4"})


async def login(c, user, realm="staff"):
    page = await c.get(f"/{realm}/login")
    data = {"email": user.email, "password": PASSWORD, "csrf_token": csrf(page)}
    if user.twofa_enabled:
        data["otp"] = pyotp.TOTP(OTP).now()
    response = await c.post(f"/{realm}/login", data=data)
    assert response.status_code == 303
    return response


def test_wave4_scoped_statements_financial_sources_and_get_read_only():
    async def scenario():
        async with fixture() as (data, conn), client() as c:
            await login(c, data["lead"])
            writes = []
            def observe(_conn, _cursor, statement, parameters, context, executemany):
                if statement.lstrip().split(" ", 1)[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
                    writes.append(statement)
            event.listen(engine.sync_engine, "before_cursor_execute", observe)
            try:
                for section in SECTIONS:
                    response = await c.get(f"{BASE}/tradespace/{section}")
                    assert response.status_code == 200, response.text[:1200]
                    assert response.headers["cache-control"].startswith("no-store")
                    assert 'data-role="teamlead"' in response.text
                    assert 'data-component="MobileNavigation"' in response.text
                    assert data["othertrader"].email not in response.text
                    assert data["foreign"].email not in response.text
                    assert "DO-NOT-EXPOSE" not in response.text
                    assert OTP not in response.text
                assert not writes, writes
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", observe)
            account = await c.get(f"{BASE}/tradespace/security")
            assert 'action="/staff/cabinet/security/password"' in account.text
            overview = await c.get(BASE)
            assert "Доступно для расчёта" in overview.text and "Долг к погашению" in overview.text
            assert "Начислено с учётом отмен" in overview.text
            assert "Отменённые начисления" in overview.text
            statement = await c.get(f"{BASE}/tradespace/accruals")
            for value in ("750.00", "650.00", "100.00", "250.00", "0.75%", "0.25%", "Отменено", "Отмена начисления"):
                assert value in statement.text
            assert "сторн" not in (overview.text + statement.text).lower()
            assert '0.750000%' not in statement.text and '0.250000%' not in statement.text
            assert "Погашение долга" in statement.text
            assert "/staff/cabinet/tradespace/operations/" not in statement.text
            only_merchant = await c.get(f"{BASE}/tradespace/accruals?source=merchant_referral&status=credited")
            assert 'data-component="TeamLeadAccrualStatement"' in only_merchant.text
            assert str(data["deposits"][1].id) not in only_merchant.text
            invalid_date = await c.get(f"{BASE}/tradespace/accruals?date_from=bad")
            assert "Проверьте даты" in invalid_date.text
            detail = await c.get(f"{BASE}/tradespace/team?member=trader:{data['trader'].id}")
            assert "История назначений" in detail.text and data["trader"].email in detail.text
            assert '0.75%' in detail.text and '0.750000%' not in detail.text
            async with client() as staff_client:
                await login(staff_client, data['super'])
                staff_accruals = await staff_client.get(f"{BASE}/tradespace/finance?tab=accruals")
                assert staff_accruals.status_code == 200
                assert '0.75%' in staff_accruals.text and '0.750000' not in staff_accruals.text
                assert "сторн" not in staff_accruals.text.lower()
                staff_trader = await staff_client.get(
                    f"{BASE}/tradespace/network?tab=traders&id={data['trader'].id}"
                )
                assert staff_trader.status_code == 200
                assert '0.75%' in staff_trader.text and '0.750000' not in staff_trader.text
            for params in (f"?member=trader:{data['othertrader'].id}", "?member=bad"):
                assert (await c.get(f"{BASE}/tradespace/team{params}")).status_code == 404
            for params in (f"?teamlead_id={data['foreign'].id}", f"?trader_id={data['othertrader'].id}"):
                response = await c.get(f"{BASE}/tradespace/accruals{params}")
                assert data["othertrader"].email not in response.text
            assert (await c.get(f"{BASE}/tradespace/operations/{data['deposits'][0].id}")).status_code == 403
            assert (await c.get(f"{BASE}/tradespace/disputes")).status_code == 403
            legacy_page = await c.get(f"{BASE}/legacy")
            assert legacy_page.status_code == 303
            assert legacy_page.headers["location"] == "/staff/cabinet"
    asyncio.run(scenario())


def test_wave4_empty_account_twofa_gates_and_staff_role_isolation():
    async def scenario():
        async with fixture() as (data, conn):
            async with client() as c:
                assert (await c.get(BASE)).headers["location"] == "/staff/login"
                assert (await c.get("/teamlead/login")).status_code == 404
                await login(c, data["empty"])
                empty = await c.get(BASE)
                assert "Финансовый счёт ещё не сформирован" in empty.text
                assert "Нет данных" in empty.text
                assert (await c.get(f"{BASE}/tradespace/team")).status_code == 200
                assert "Участники не найдены" in (await c.get(f"{BASE}/tradespace/team")).text
                assert not await conn.scalar(select(TeamLeadBalance.id).where(TeamLeadBalance.teamlead_id == data["empty"].id))
                assert (await c.get("/trader/cabinet")).status_code in {303,403}
            async with client() as c:
                await login(c, data["gated"])
                for section in ("team", "accruals", "settlements"):
                    page = await c.get(f"{BASE}/tradespace/{section}")
                    assert "Для этой роли обязательна 2FA" in page.text
                    assert 'data-component="TeamLeadAccrualStatement"' not in page.text
            for key in ("admin", "support", "super"):
                async with client() as c:
                    await login(c, data[key])
                    for section in ("team", "accruals"):
                        assert (await c.get(f"{BASE}/tradespace/{section}")).status_code == 403
            async with client() as c:
                await login(c, data["foreign"])
                page = await c.get(f"{BASE}/tradespace/accruals")
                assert data["trader"].email not in page.text
                assert data["merchant"].name not in page.text
                assert data["othertrader"].email in page.text
    asyncio.run(scenario())


def test_wave4_existing_settlement_command_csrf_idempotency_and_feedback(monkeypatch):
    async def fake_quote():
        return quote()
    monkeypatch.setattr(legacy, "get_strict_rolling_ask_quote", fake_quote)
    async def scenario():
        async with fixture() as (data, conn), client() as c:
            await login(c, data["lead"])
            page = await c.get(f"{BASE}/tradespace/settlements")
            assert "Комиссия 5.00 USDT" in page.text and "168" in page.text
            assert "Выполнен" in page.text and "Отклонён" in page.text
            assert "2.12" in page.text and "100.00000000" in page.text
            assert "+ 5.00 USDT комиссия" in page.text
            assert "5.000000 USDT" not in page.text
            key = re.search(r'name="idempotency_key" value="([^"]+)"', page.text).group(1)
            path = f"{BASE}/teamlead/settlements/request"
            missing = await c.post(path, data={"requested_usdt":"1", "wallet_address":WALLET, "idempotency_key":key})
            assert missing.status_code == 403
            invalid = await c.post(path, data={"requested_usdt":"-1", "wallet_address":WALLET,
                "idempotency_key":key, "csrf_token":csrf(page)})
            assert invalid.status_code == 303
            payload = {"requested_usdt":"1.123456", "wallet_address":WALLET,
                "idempotency_key":key, "csrf_token":csrf(page)}
            first = await c.post(path, data=payload)
            assert first.status_code == 303
            repeated = await c.post(path, data=payload)
            assert repeated.status_code == 303
            count = await conn.scalar(select(func.count(TeamLeadSettlement.id)).where(
                TeamLeadSettlement.idempotency_key == key))
            assert count == 1
            stored = await conn.scalar(select(TeamLeadSettlement.requested_usdt).where(
                TeamLeadSettlement.idempotency_key == key))
            assert stored == Decimal("1.123456")
            updated = await c.get(first.headers["location"])
            assert "На рассмотрении" in updated.text
            assert "1.12" in updated.text
            assert "Предыдущий запрос на рассмотрении" in updated.text
            assert 'data-dialog-open="teamlead-settlement-confirm"' not in updated.text
            assert str(data["foreign"].id) not in updated.text
    asyncio.run(scenario())
