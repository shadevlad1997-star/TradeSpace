"""Trader requisite soft-delete visibility and historical-reference regression."""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models as m
from app.core.security import encrypt_secret
from tests.test_acceptance_financial_postgres import request
from tests.test_integration_modes_postgres import client, isolated
from tests.tradespace.test_wave4_teamlead import PASSWORD, csrf, login


def test_soft_deleted_requisites_leave_lists_but_keep_history_and_routing_guards(monkeypatch):
    async def scenario():
        suffix = uuid.uuid4().hex
        target_label = f"DELETE TARGET {suffix}"
        target_value = f"+79991{suffix[-6:]}"
        qr_label = f"DELETE QR {suffix}"
        remaining_label = f"REMAINING {suffix}"
        history_external_id = f"HISTORY-{suffix}"

        async with isolated(monkeypatch) as (engine, merchant, users, keys), client() as trader_client:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                target = await db.scalar(
                    select(m.Requisite).where(m.Requisite.trader_id == users["trader"].id)
                )
                target.owner_name = target.full_name = target_label
                target.value_encrypted = encrypt_secret(target_value)
                target_id = target.id

                remaining = m.Requisite(
                    trader_id=users["trader"].id,
                    owner_name=remaining_label,
                    full_name=remaining_label,
                    method="sbp",
                    bank_code="tbank",
                    bank_name="Т-Банк",
                    value_encrypted=encrypt_secret(f"+79992{suffix[-6:]}"),
                    enabled=True,
                    status="active",
                    min_check=Decimal("1"),
                    max_check=Decimal("100000"),
                    daily_limit=Decimal("1000000"),
                    simultaneous_limit=10,
                    request_count=100,
                    operation_limit=100,
                )
                qr_payload = "https://qr.nspk.ru/AS" + "0" * 30
                qr = m.Requisite(
                    trader_id=users["trader"].id,
                    owner_name=qr_label,
                    full_name=qr_label,
                    method="sbp_qr",
                    bank_code="alfabank",
                    bank_name="Альфа-Банк",
                    value_encrypted=encrypt_secret(qr_payload),
                    qr_identity=qr_payload.rsplit("/", 1)[1],
                    enabled=True,
                    status="active",
                    min_check=Decimal("1"),
                    max_check=Decimal("100000"),
                    daily_limit=Decimal("1000000"),
                    simultaneous_limit=10,
                    request_count=100,
                    operation_limit=100,
                )
                other = m.User(
                    email=f"other-{suffix}@example.test",
                    password_hash=users["trader"].password_hash,
                    role="trader",
                    trader_balance=Decimal("100000"),
                )
                db.add_all([remaining, qr, other])
                await db.flush()
                foreign = m.Requisite(
                    trader_id=other.id,
                    owner_name=f"FOREIGN {suffix}",
                    full_name=f"FOREIGN {suffix}",
                    method="sbp",
                    bank_code="sberbank",
                    bank_name="СберБанк",
                    value_encrypted=encrypt_secret(f"+79993{suffix[-6:]}"),
                    enabled=True,
                    status="active",
                    min_check=Decimal("1"),
                    max_check=Decimal("100000"),
                    daily_limit=Decimal("1000000"),
                    simultaneous_limit=10,
                    request_count=100,
                    operation_limit=100,
                )
                db.add(foreign)
                await db.flush()
                deposit = m.Deposit(
                    merchant_id=merchant.id,
                    external_id=history_external_id,
                    idempotency_key=history_external_id,
                    amount=Decimal("1000"),
                    currency="RUB",
                    method="sbp",
                    status="paid",
                    requisites_id=target.id,
                    expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                    metadata_json={},
                )
                db.add(deposit)
                await db.commit()
                qr_id, remaining_id, foreign_id, deposit_id = qr.id, remaining.id, foreign.id, deposit.id

            await login(trader_client, users["trader"], realm="trader")
            page = await trader_client.get("/trader/cabinet/tradespace/requisites")
            assert page.status_code == 200
            assert target_label in page.text and qr_label in page.text and remaining_label in page.text
            assert f"FOREIGN {suffix}" not in page.text

            # CSRF and ownership failures keep the target and explain the denial.
            denied_csrf = await trader_client.post(
                f"/trader/cabinet/requisites/{target_id}/delete"
            )
            assert denied_csrf.status_code == 403
            denied_owner = await trader_client.post(
                f"/trader/cabinet/requisites/{foreign_id}/delete",
                data={"csrf_token": csrf(page)},
                follow_redirects=False,
            )
            assert denied_owner.status_code == 303
            assert "ui_error=requisite_not_found" in denied_owner.headers["location"]
            denial_page = await trader_client.get(denied_owner.headers["location"])
            assert "Реквизит не найден" in denial_page.text
            still_present = await trader_client.get("/trader/cabinet/tradespace/requisites")
            assert target_label in still_present.text

            deleted = await trader_client.post(
                f"/trader/cabinet/requisites/{target_id}/delete",
                data={"csrf_token": csrf(still_present)},
                follow_redirects=True,
            )
            assert deleted.status_code == 200
            assert target_label not in deleted.text
            assert "Удалено" not in deleted.text
            assert remaining_label in deleted.text

            refreshed = await trader_client.get(
                f"/trader/cabinet/tradespace/requisites?query={suffix}&page=2"
            )
            assert target_label not in refreshed.text
            assert remaining_label in refreshed.text

            qr_page = await trader_client.get("/trader/cabinet/tradespace/requisites")
            deleted_qr = await trader_client.post(
                f"/trader/cabinet/requisites/{qr_id}/delete",
                data={"csrf_token": csrf(qr_page)},
                follow_redirects=True,
            )
            assert deleted_qr.status_code == 200
            assert qr_label not in deleted_qr.text
            assert "Доступно: 1" in deleted_qr.text

            # A new session and history retain the same server-side result.
            async with client() as fresh:
                await login(fresh, users["trader"], realm="trader")
                fresh_page = await fresh.get("/trader/cabinet/tradespace/requisites")
                assert target_label not in fresh_page.text and qr_label not in fresh_page.text
                history = await fresh.get(
                    "/trader/cabinet/tradespace/history",
                    params={"deposit_query": target_label},
                )
                assert history.status_code == 200
                assert history_external_id in history.text
                detail = await fresh.get(
                    f"/trader/cabinet/tradespace/operations/{deposit_id}"
                )
                assert detail.status_code == 200
                assert target_value in detail.text

            async with AsyncSession(engine, expire_on_commit=False) as db:
                deleted_row = await db.get(m.Requisite, target_id)
                deleted_qr_row = await db.get(m.Requisite, qr_id)
                foreign_row = await db.get(m.Requisite, foreign_id)
                assert deleted_row is not None and deleted_row.status == "deleted" and not deleted_row.enabled
                assert deleted_qr_row is not None and deleted_qr_row.status == "deleted" and not deleted_qr_row.enabled
                assert foreign_row.status == "active" and foreign_row.enabled
                assert (await db.get(m.Deposit, deposit_id)).requisites_id == target_id
                assert await db.scalar(
                    select(func.count()).select_from(m.AuditLog).where(
                        m.AuditLog.target_id.in_([str(target_id), str(qr_id)]),
                        m.AuditLog.action == "requisite_deleted_by_trader",
                    )
                ) == 2

                # Leave no usable SBP candidate: a deleted requisite must not be routed.
                (await db.get(m.Requisite, remaining_id)).enabled = False
                foreign_row.enabled = False
                before_count = await db.scalar(select(func.count()).select_from(m.Deposit))
                await db.commit()

            api_key, api_secret = keys["sandbox"]
            unavailable = await request(api_key, api_secret, f"NO-DELETED-{suffix}")
            assert unavailable.status_code == 409, unavailable.text
            async with AsyncSession(engine) as db:
                assert await db.scalar(select(func.count()).select_from(m.Deposit)) == before_count
                assert (await db.get(m.User, users["trader"].id)).trader_hold == 0

    asyncio.run(scenario())
