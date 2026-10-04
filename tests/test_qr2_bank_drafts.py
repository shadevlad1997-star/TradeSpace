"""Inactive bank settings: PostgreSQL, real browser commands, no bank transport."""
import asyncio
import re
import uuid

import httpx
import psycopg
import pytest
from psycopg import sql
from sqlalchemy import select, func, event, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import models as m
from app.core.security import encrypt_secret, hash_password
from app.services import bank_connections as drafts
from tests.test_appeal_confirmation_rule_postgres import isolated
from tests.test_integration_modes_postgres import client
from tests.tradespace.test_wave4_teamlead import login, csrf, PASSWORD
from tests.test_qr1 import prepare, create, result, confirm
from tests.test_migrations_postgres import _database_urls, _alembic

PAGE = "/trader/cabinet/tradespace/bank-integrations"
COMMAND = "/trader/cabinet/bank-integrations"


def form(page, **fields):
    match = re.search(r'name="session_view" value="([^"]+)"', page.text)
    assert match
    return {"csrf_token": csrf(page), "session_view": match.group(1), **fields}


async def rows(engine):
    async with AsyncSession(engine) as db:
        return list(await db.scalars(select(m.TraderBankConnection).order_by(m.TraderBankConnection.created_at)))


async def reqs(engine, trader):
    async with AsyncSession(engine, expire_on_commit=False) as db:
        other = m.User(email=f"other-{uuid.uuid4().hex}@example.test", role="trader", password_hash=hash_password(PASSWORD))
        db.add(other); await db.flush()
        result = {}
        for label, owner, method, bank in [("own", trader.id, "sbp_qr", "alfabank"),
                ("modul", trader.id, "sbp_qr", "modulbank"), ("wrong_method", trader.id, "sbp", "alfabank"),
                ("foreign", other.id, "sbp_qr", "alfabank")]:
            row = m.Requisite(trader_id=owner, method=method, bank_code=bank,
                owner_name="Synthetic", value_encrypted=encrypt_secret("synthetic QR"))
            db.add(row); await db.flush(); result[label] = row.id
        await db.commit()
        return other, result


def test_draft_crud_persistence_no_network_or_financial_writes(monkeypatch):
    async def run():
        async with isolated(monkeypatch) as (engine, merchant, users, keys), client() as c:
            _, req = await reqs(engine, users["trader"])
            await login(c, users["trader"], "trader")
            def no_network(*args, **kwargs):
                raise AssertionError("Draft management must not open HTTP transport")
            monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)
            monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
            writes = []
            def observe(conn, cursor, statement, parameters, context, executemany):
                if statement.lstrip().split(" ", 1)[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
                    writes.append(statement.lower())
            event.listen(engine.sync_engine, "before_cursor_execute", observe)
            try:
                page = await c.get(PAGE)
                assert page.status_code == 200 and "Банки пока не подключены" in page.text
                assert not writes
                created = await c.post(COMMAND + "/create", data=form(page, bank_code="alfabank", name="Первый", requisite_id=str(req["own"])))
                assert created.status_code == 303
                page = await c.get(created.headers["location"])
                assert "Первый" in page.text and "Не подключён" in page.text
                first = (await rows(engine))[0]
                assert first.requisite_id == req["own"] and first.status == "not_connected"
                for bank in ("alfabank", "modulbank"):
                    response = await c.post(COMMAND + "/create", data=form(page, bank_code=bank, name="Второй"))
                    assert response.status_code == 303
                assert len(await rows(engine)) == 3
                edited = await c.post(f"{COMMAND}/{first.id}/edit", data=form(page, bank_code="modulbank", name="Изменён", requisite_id=str(req["modul"])))
                assert edited.status_code == 303
                await c.post("/trader/logout", data=form(page))
                assert (await c.get(PAGE)).status_code == 303
                await login(c, users["trader"], "trader")
                page = await c.get(PAGE)
                assert "Изменён" in page.text
                editing = await c.get(PAGE + f"?edit={first.id}")
                assert 'value="Изменён"' in editing.text and 'value="modulbank" selected' in editing.text
                assert str(req["modul"]) in editing.text
                deleted = await c.post(f"{COMMAND}/{first.id}/delete", data=form(page))
                assert deleted.status_code == 303
                assert len(await rows(engine)) == 2
                async with AsyncSession(engine) as db:
                    assert await db.get(m.Requisite, req["modul"]) is not None
                    actions = list(await db.scalars(select(m.AuditLog.action).where(m.AuditLog.target_type == "bank_connection")))
                    assert sorted(actions) == sorted(["bank_draft_created"] * 3 + ["bank_draft_updated", "bank_draft_deleted"])
                assert all(any(table in s for table in ("trader_bank_connections", "audit_logs", "users")) for s in writes), writes
                # Login may update User auth counters; draft endpoints must never write money tables.
                assert not any(any(table in s for table in (" deposits ", " balances ", "ledger_entries", "requisites set", "fee_snapshots")) for s in writes)
                before = len(writes)
                for suffix in ("", "?new=1"):
                    response = await c.get(PAGE + suffix)
                    assert response.headers["cache-control"] == "no-store, max-age=0"
                assert len(writes) == before
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", observe)
    asyncio.run(run())


def test_ownership_bank_method_validation_csrf_and_role_isolation(monkeypatch):
    async def run():
        async with isolated(monkeypatch) as (engine, _, users, _), client() as c:
            other, req = await reqs(engine, users["trader"])
            async with AsyncSession(engine, expire_on_commit=False) as db:
                foreign = await drafts.save_draft(db, other, bank_code="alfabank", name="PRIVATE-OTHER-DRAFT", requisite_id=str(req["foreign"]))
                await db.commit(); foreign_id = foreign.id
            await login(c, users["trader"], "trader")
            page = await c.get(PAGE + "?new=1")
            assert "PRIVATE-OTHER-DRAFT" not in page.text and str(req["foreign"]) not in page.text
            assert (await c.post(COMMAND + "/create", data={"bank_code":"alfabank", "name":"No CSRF"})).status_code == 403
            for suffix in (f"?edit={foreign_id}", "?edit=bad-id"):
                assert (await c.get(PAGE + suffix)).status_code == 404
            for action in ("edit", "delete"):
                response = await c.post(f"{COMMAND}/{foreign_id}/{action}", data=form(page, bank_code="alfabank", name="attempt"))
                assert response.status_code == 404
            for bad in (req["foreign"], req["wrong_method"], req["modul"], uuid.uuid4(), "bad-id"):
                response = await c.post(COMMAND + "/create", data=form(page, bank_code="alfabank", name="Kept name", requisite_id=str(bad)))
                assert response.status_code == 422 and "Выберите свой реквизит QR NSPK" in response.text
                assert 'value="Kept name"' in response.text
            for bank, name in (("sberbank", "Test"), ("alfabank", "   "), ("alfabank", "x" * 121)):
                assert (await c.post(COMMAND + "/create", data=form(page, bank_code=bank, name=name))).status_code == 422
            stale = form(page, bank_code="alfabank", name="Stale")
            stale["session_view"] = "wrong-session"
            assert (await c.post(COMMAND + "/create", data=stale)).status_code == 409
            assert len(await rows(engine)) == 1
            for role in ("merchant", "admin", "superadmin", "support", "teamlead", "aggregator", "operator"):
                if role == "operator":
                    async with AsyncSession(engine, expire_on_commit=False) as db:
                        user = m.User(email=f"operator-{uuid.uuid4().hex}@example.test", role=role, password_hash=hash_password(PASSWORD))
                        db.add(user); await db.commit()
                else:
                    user = users[role]
                realm = "staff" if role in {"admin", "superadmin", "support", "teamlead"} else ("trader" if role == "operator" else role)
                async with client() as denied:
                    await login(denied, user, realm)
                    home = await denied.get(f"/{realm}/cabinet/tradespace/security")
                    assert "Интеграция с банком" not in home.text
                    assert (await denied.get(f"/{realm}/cabinet/tradespace/bank-integrations")).status_code == 403
                    for path in ("create", f"{foreign_id}/edit", f"{foreign_id}/delete"):
                        response = await denied.post(f"/{realm}/cabinet/bank-integrations/{path}", data=form(home, bank_code="alfabank", name="No"))
                        assert response.status_code == 403, (role, response.text)
            assert len(await rows(engine)) == 1
    asyncio.run(run())


@pytest.mark.parametrize("origin", ["merchant", "aggregator"])
def test_drafts_do_not_change_issued_static_qr_or_finances(monkeypatch, origin):
    async def run():
        async with isolated(monkeypatch) as (engine, merchant, users, keys), client() as c:
            agg = await prepare(engine, merchant, users, origin)
            async with AsyncSession(engine) as db:
                req = await db.scalar(select(m.Requisite))
                req.bank_code = "alfabank"
                await db.commit()
            async with AsyncSession(engine) as db:
                req_id = await db.scalar(select(m.Requisite.id))
            oid, qr = result(await create(c, origin, keys, agg), origin)
            async def state():
                async with AsyncSession(engine) as db:
                    dep = await db.get(m.Deposit, oid)
                    user = await db.get(m.User, users["trader"].id)
                    req = await db.get(m.Requisite, req_id)
                    fee = await db.scalar(select(m.OperationFeeSnapshot).where(m.OperationFeeSnapshot.deposit_id == oid))
                    return (dep.status, dep.qr_snapshot, dep.qr_reserved, dep.qr_identity, user.trader_balance,
                        user.trader_hold, req.value_encrypted, req.qr_identity, req.status,
                        {col.name: getattr(fee, col.name) for col in m.OperationFeeSnapshot.__table__.columns})
            before = await state()
            async with AsyncSession(engine, expire_on_commit=False) as db:
                row = await drafts.save_draft(db, users["trader"], bank_code="alfabank", name="Static QR future", requisite_id=str(req_id))
                await db.commit(); draft_id = row.id
                await drafts.save_draft(db, users["trader"], bank_code="modulbank", name="Unlinked", draft_id=draft_id)
                await db.commit()
                await drafts.delete_draft(db, users["trader"], draft_id)
                await db.commit()
            assert await state() == before
            page = await c.get(qr["checkout_url"])
            assert page.status_code == 200
            assert (await confirm(engine, oid, users["trader"])).confirmed
            after = await state()
            assert after[0] == "paid" and after[2] is False and after[5] == 0
    asyncio.run(run())


def test_bank_draft_migration_upgrade_constraints_and_requisite_preservation():
    admin, url, name = _database_urls()
    with psycopg.connect(admin, autocommit=True) as db:
        db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        upgrade = _alembic(url, "upgrade", "head")
        assert upgrade.returncode == 0, upgrade.stderr
        user, req, draft = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        seed_engine = create_engine(url.replace("postgresql://", "postgresql+psycopg://"))
        with Session(seed_engine) as seed_db:
            seed_db.add(m.User(id=user, email="synthetic@example.test", password_hash="unused", role="trader"))
            seed_db.flush()
            seed_db.add(m.Requisite(id=req, trader_id=user, owner_name="Synthetic", method="sbp_qr", value_encrypted="synthetic", bank_code="alfabank"))
            seed_db.flush()
            seed_db.add(m.TraderBankConnection(id=draft, trader_id=user, name="Test", bank_code="alfabank", requisite_id=req))
            seed_db.commit()
        seed_engine.dispose()
        with psycopg.connect(url) as db:
            assert db.execute("SELECT status FROM trader_bank_connections").fetchone()[0] == "not_connected"
            with pytest.raises(psycopg.errors.CheckViolation):
                db.execute("UPDATE trader_bank_connections SET status='connected'")
            db.rollback()
            db.execute("DELETE FROM trader_bank_connections WHERE id=%s", (draft,)); db.commit()
            assert db.execute("SELECT id FROM requisites").fetchone()[0] == req
        check = _alembic(url, "check")
        assert check.returncode == 0, check.stdout + check.stderr
        down = _alembic(url, "downgrade", "0028_static_nspk_qr")
        assert down.returncode == 0, down.stderr
        with psycopg.connect(url) as db:
            assert db.execute("SELECT id FROM requisites").fetchone()[0] == req
        assert _alembic(url, "upgrade", "head").returncode == 0
    finally:
        with psycopg.connect(admin, autocommit=True) as db:
            db.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
