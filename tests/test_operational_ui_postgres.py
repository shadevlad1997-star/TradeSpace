import asyncio
import html
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pyotp
import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.enums import DepositStatus, Role
from app.core.security import encrypt_secret, hash_password
from app.db.session import engine as application_engine
from app.main import app
from app.models import Deposit, Merchant, Requisite, User, WebhookDeliveryAttempt, WebhookEvent
from app.web.routes import _load_trader_deposit_view


def _database_url() -> str:
    value = os.getenv("TEST_DATABASE_URL", "")
    if not value:
        pytest.skip("TEST_DATABASE_URL is required for operational UI tests")
    if "postgresql" not in value:
        pytest.fail("Operational UI integration tests require PostgreSQL")
    if value.startswith("postgresql://"):
        return value.replace("postgresql://", "postgresql+asyncpg://", 1)
    return value


def _csrf(response: httpx.Response) -> str:
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def test_merchant_webhook_page_batches_scoped_attempts_without_mutation():
    async def scenario():
        await application_engine.dispose(close=False)
        engine = create_async_engine(_database_url())
        suffix = uuid.uuid4().hex
        password = f"Merchant-webhook-{suffix}"
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                owner_a = User(
                    email=f"webhook-a-{suffix}@example.test",
                    password_hash=hash_password(password),
                    role=Role.merchant.value,
                )
                owner_b = User(
                    email=f"webhook-b-{suffix}@example.test",
                    password_hash=hash_password(password),
                    role=Role.merchant.value,
                )
                db.add_all([owner_a, owner_b])
                await db.flush()
                merchant_a = Merchant(owner_id=owner_a.id, name=f"Merchant A {suffix}")
                merchant_b = Merchant(owner_id=owner_b.id, name=f"Merchant B {suffix}")
                db.add_all([merchant_a, merchant_b])
                await db.flush()
                marker_a = f"VISIBLE-A-{suffix}"
                marker_b = f"FORBIDDEN-B-{suffix}"
                event_a = WebhookEvent(
                    merchant_id=merchant_a.id,
                    event_type="deposit.paid",
                    payload={
                        "operation_type": "deposit",
                        "external_id": marker_a,
                        "status": "paid",
                        "amount": "1250.00",
                        "currency": "RUB",
                        "api_key": f"API-SECRET-{suffix}",
                        "note": "<script>alert(1)</script>",
                    },
                    status="failed",
                    attempts=1,
                    last_error=f"Authorization: EVENT-SECRET-{suffix}",
                    last_status_code=500,
                )
                event_b = WebhookEvent(
                    merchant_id=merchant_b.id,
                    event_type="deposit.failed",
                    payload={"external_id": marker_b, "status": "failed"},
                    status="failed",
                    attempts=1,
                )
                db.add_all([event_a, event_b])
                await db.flush()
                attempt_a = WebhookDeliveryAttempt(
                    webhook_event_id=event_a.id,
                    attempt_no=1,
                    status="failed",
                    status_code=500,
                    error=f"Cookie: ATTEMPT-SECRET-{suffix}",
                    response_snippet='{"message":"<img src=x onerror=alert(1)>","token":"hidden"}',
                )
                attempt_b = WebhookDeliveryAttempt(
                    webhook_event_id=event_b.id,
                    attempt_no=1,
                    status="failed",
                    status_code=500,
                    response_snippet=marker_b,
                )
                db.add_all([attempt_a, attempt_b])
                await db.commit()
                event_a_id = event_a.id
                event_b_id = event_b.id
                attempt_a_id = attempt_a.id

            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://localhost",
                follow_redirects=False,
            ) as client:
                login_page = await client.get("/merchant/login")
                login = await client.post(
                    "/merchant/login",
                    data={
                        "csrf_token": _csrf(login_page),
                        "email": owner_a.email,
                        "password": password,
                    },
                )
                assert login.status_code == 303

                attempt_queries = []

                def count_attempt_query(_conn, _cursor, statement, _parameters, _context, _many):
                    if "from webhook_delivery_attempts" in statement.lower():
                        attempt_queries.append(statement)

                event.listen(
                    application_engine.sync_engine,
                    "before_cursor_execute",
                    count_attempt_query,
                )
                try:
                    page = await client.get("/merchant/cabinet?section=webhook")
                finally:
                    event.remove(
                        application_engine.sync_engine,
                        "before_cursor_execute",
                        count_attempt_query,
                    )

            assert page.status_code == 200, page.text[:1000]
            assert len(attempt_queries) == 1
            assert marker_a in page.text
            assert marker_b not in page.text
            assert str(event_b_id) not in page.text
            assert f"API-SECRET-{suffix}" not in page.text
            assert f"EVENT-SECRET-{suffix}" not in page.text
            assert f"ATTEMPT-SECRET-{suffix}" not in page.text
            # The product contract is stricter than redaction: free-form remote
            # errors and payloads are not exposed. Delivery metadata remains visible.
            assert "Попытка 1" in page.text
            assert "HTTP 500" in page.text
            assert '"token":"hidden"' not in page.text
            assert "hidden" not in html.unescape(page.text).split('ts-webhook-feed', 1)[1].split('</section>', 1)[0]
            assert "<script>alert(1)</script>" not in page.text
            assert "&lt;script&gt;alert(1)&lt;/script&gt;" not in page.text
            assert "<img src=x onerror=alert(1)>" not in page.text
            assert "&lt;img src=x onerror=alert(1)&gt;" not in page.text
            assert 'class="ts-webhook-event"' in page.text

            async with AsyncSession(engine) as verify:
                persisted_event = await verify.get(WebhookEvent, event_a_id)
                persisted_attempt = await verify.get(WebhookDeliveryAttempt, attempt_a_id)
                assert persisted_event is not None
                assert persisted_event.status == "failed"
                assert persisted_event.attempts == 1
                assert persisted_attempt is not None
                assert persisted_attempt.status == "failed"
                assert await verify.scalar(
                    select(WebhookEvent.id).where(WebhookEvent.id == event_b_id)
                ) == event_b_id
        finally:
            await application_engine.dispose()
            await engine.dispose()

    asyncio.run(scenario())


def test_trader_deposit_views_filters_scope_and_aggregates_are_read_only():
    async def scenario():
        await application_engine.dispose(close=False)
        engine = create_async_engine(_database_url())
        suffix = uuid.uuid4().hex
        password = f"Trader-queue-{suffix}"
        now = datetime.now(timezone.utc)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                trader = User(
                    email=f"queue-trader-{suffix}@example.test",
                    password_hash=hash_password(password),
                    role=Role.trader.value,
                    trader_balance=Decimal("9000.00"),
                    trader_hold=Decimal("1250.00"),
                )
                other_trader = User(
                    email=f"queue-other-{suffix}@example.test",
                    password_hash=hash_password(password),
                    role=Role.trader.value,
                )
                merchant_owner = User(
                    email=f"queue-merchant-{suffix}@example.test",
                    password_hash=hash_password(password),
                    role=Role.merchant.value,
                )
                otp_secret = pyotp.random_base32()
                superadmin = User(
                    email=f"queue-superadmin-{suffix}@example.test",
                    password_hash=hash_password(password),
                    role=Role.superadmin.value,
                    twofa_enabled=True,
                    twofa_secret=encrypt_secret(otp_secret),
                )
                admin = User(
                    email=f"queue-admin-{suffix}@example.test",
                    password_hash=hash_password(password),
                    role=Role.admin.value,
                )
                support = User(
                    email=f"queue-support-{suffix}@example.test",
                    password_hash=hash_password(password),
                    role=Role.support.value,
                )
                db.add_all([
                    trader, other_trader, merchant_owner, superadmin, admin, support,
                ])
                await db.flush()
                merchant = Merchant(owner_id=merchant_owner.id, name=f"Queue merchant {suffix}")
                db.add(merchant)
                await db.flush()
                requisite = Requisite(
                    trader_id=trader.id,
                    owner_name="Queue Owner",
                    full_name="Queue Owner",
                    method="sbp",
                    value_encrypted=encrypt_secret(f"+7999000{suffix[-4:]}"),
                    bank_code="sberbank",
                    bank_name="СберБанк",
                    daily_limit=Decimal("100000.00"),
                    operation_limit=10,
                    simultaneous_limit=2,
                    min_check=Decimal("100.00"),
                    max_check=Decimal("50000.00"),
                )
                other_requisite = Requisite(
                    trader_id=other_trader.id,
                    owner_name="Other Owner",
                    full_name="Other Owner",
                    method="sbp",
                    value_encrypted=encrypt_secret(f"+7888000{suffix[-4:]}"),
                    bank_code="sberbank",
                    bank_name="СберБанк",
                )
                db.add_all([requisite, other_requisite])
                await db.flush()

                markers = {
                    "urgent": f"ACTIVE-URGENT-{suffix}",
                    "later": f"ACTIVE-LATER-{suffix}",
                    "no_deadline": f"ACTIVE-NO-DEADLINE-{suffix}",
                    "paid": f"HISTORY-PAID-{suffix}",
                    "failed": f"HISTORY-FAILED-{suffix}",
                    "other": f"FORBIDDEN-OTHER-{suffix}",
                }

                def deposit(marker, status, req, expires, amount="1000.00"):
                    return Deposit(
                        merchant_id=merchant.id,
                        external_id=marker,
                        idempotency_key=marker,
                        amount=Decimal(amount),
                        currency="RUB",
                        method="sbp",
                        status=status,
                        requisites_id=req.id,
                        metadata_json={},
                        expires_at=expires,
                    )

                rows = [
                    deposit(markers["later"], DepositStatus.pending.value, requisite, now + timedelta(minutes=12)),
                    deposit(markers["urgent"], DepositStatus.pending.value, requisite, now + timedelta(minutes=3)),
                    deposit(markers["no_deadline"], DepositStatus.pending.value, requisite, None),
                    deposit(markers["paid"], DepositStatus.paid.value, requisite, now + timedelta(minutes=15), "2500.00"),
                    deposit(markers["failed"], DepositStatus.failed.value, requisite, now + timedelta(minutes=15)),
                    deposit(markers["other"], DepositStatus.pending.value, other_requisite, now + timedelta(minutes=2)),
                ]
                db.add_all(rows)
                await db.commit()
                base_filters = {
                    "view": "active", "query": "", "amount": "", "requisite": "",
                    "date": "", "bank_code": "", "payment_method": "", "status": "",
                }
                active_first_page = await _load_trader_deposit_view(
                    db,
                    subject_trader_id=trader.id,
                    filters=base_filters,
                    page_size=1,
                )
                history_first_page = await _load_trader_deposit_view(
                    db,
                    subject_trader_id=trader.id,
                    filters={**base_filters, "view": "history"},
                    page_size=1,
                )
                assert len(active_first_page["deposits"]) == 1
                assert active_first_page["deposits"][0].external_id == markers["urgent"]
                assert active_first_page["aggregates"]["active_count"] == 3
                assert active_first_page["aggregates"]["history_count"] == 2
                assert len(history_first_page["deposits"]) == 1
                assert history_first_page["deposits"][0].status in {
                    DepositStatus.paid.value,
                    DepositStatus.failed.value,
                }
                trader_id = trader.id
                row_ids = [row.id for row in rows]

            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://localhost",
                follow_redirects=False,
            ) as client:
                login_page = await client.get("/trader/login")
                login = await client.post(
                    "/trader/login",
                    data={
                        "csrf_token": _csrf(login_page),
                        "email": trader.email,
                        "password": password,
                    },
                )
                assert login.status_code == 303
                active = await client.get("/trader/cabinet?section=deposits&view=active")
                history = await client.get("/trader/cabinet?section=deposits&view=history")
                active_polls = [
                    await client.get("/trader/cabinet/partials/deposits?view=active")
                    for _ in range(2)
                ]
                history_polls = [
                    await client.get("/trader/cabinet/partials/deposits?view=history")
                    for _ in range(2)
                ]
                filtered = await client.get(
                    "/trader/cabinet?section=deposits&view=history"
                    "&bank_code=sberbank&payment_method=sbp&status=paid"
                )
                filtered_poll = await client.get(
                    "/trader/cabinet/partials/deposits?view=history"
                    "&bank_code=sberbank&payment_method=sbp&status=paid"
                )
                forbidden_preview = await client.get("/trader/cabinet/superadmin")

            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://localhost",
                follow_redirects=False,
            ) as staff_client:
                login_page = await staff_client.get("/staff/login")
                login = await staff_client.post(
                    "/staff/login",
                    data={
                        "csrf_token": _csrf(login_page),
                        "email": superadmin.email,
                        "password": password,
                        "otp": pyotp.TOTP(otp_secret).now(),
                    },
                )
                assert login.status_code == 303
                staff_cabinet = await staff_client.get(
                    "/staff/cabinet/tradespace/network?tab=traders&q=" + str(trader.id)
                )
                preview_link_match = re.search(
                    rf'data-preview-trader="{trader.id}" href="([^"]*preview_context=[^"]+)"',
                    staff_cabinet.text,
                )
                assert preview_link_match
                preview_entry_url = html.unescape(preview_link_match.group(1))
                preview_active = await staff_client.get(preview_entry_url)
                session_view_match = re.search(
                    r'data-session-view="([0-9a-f]+)"',
                    preview_active.text,
                )
                assert session_view_match
                preview_headers = {"x-session-view": session_view_match.group(1)}
                preview_history = await staff_client.get(
                    "/staff/cabinet/trader?section=deposits&view=history"
                )
                preview_active_polls = [
                    await staff_client.get(
                        "/staff/cabinet/partials/deposits?view=active",
                        headers=preview_headers,
                    )
                    for _ in range(2)
                ]
                preview_history_polls = [
                    await staff_client.get(
                        "/staff/cabinet/partials/deposits?view=history",
                        headers=preview_headers,
                    )
                    for _ in range(2)
                ]
                tampered_preview = await staff_client.get(
                    f"/staff/cabinet/trader?section=deposits&view=active"
                    f"&subject_trader_id={other_trader.id}"
                )
                clear_preview = await staff_client.get("/staff/cabinet")
                missing_preview = await staff_client.get(
                    "/staff/cabinet/trader?section=deposits&view=active"
                )
                preview_token_match = re.search(
                    r"preview_context=([^&]+)", preview_entry_url
                )
                assert preview_token_match
                preview_token = preview_token_match.group(1)
                token_parts = preview_token.split(".")
                assert token_parts[-1]
                token_parts[-1] = (
                    ("a" if token_parts[-1][0] != "a" else "b")
                    + token_parts[-1][1:]
                )
                invalid_token = ".".join(token_parts)
                invalid_preview = await staff_client.get(
                    preview_entry_url.replace(preview_token, invalid_token, 1)
                )

            unauthorized_previews = []
            for staff_user in (admin, support):
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="https://localhost",
                    follow_redirects=False,
                ) as unauthorized_client:
                    login_page = await unauthorized_client.get("/staff/login")
                    login = await unauthorized_client.post(
                        "/staff/login",
                        data={
                            "csrf_token": _csrf(login_page),
                            "email": staff_user.email,
                            "password": password,
                        },
                    )
                    assert login.status_code == 303
                    unauthorized_previews.append(await unauthorized_client.get(
                        "/staff/cabinet/trader?section=deposits&view=active"
                    ))
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://localhost",
                follow_redirects=False,
            ) as merchant_client:
                login_page = await merchant_client.get("/merchant/login")
                login = await merchant_client.post(
                    "/merchant/login",
                    data={
                        "csrf_token": _csrf(login_page),
                        "email": merchant_owner.email,
                        "password": password,
                    },
                )
                assert login.status_code == 303
                unauthorized_previews.append(await merchant_client.get(
                    "/merchant/cabinet/trader?section=deposits&view=active"
                ))

            assert active.status_code == history.status_code == filtered.status_code == 200
            assert 'data-component="TraderWorkbench"' in active.text
            assert 'data-component="FilterBar"' in history.text
            assert all(response.status_code == 200 for response in active_polls + history_polls)
            assert filtered_poll.status_code == 200
            assert forbidden_preview.status_code == 403
            # Workbench also includes recent history below the active queue.
            # Apply the active/terminal split to the actual queue, not the whole dashboard.
            active_queue = re.search(r'<aside class="ts-work-queue".*?</aside>', active.text, re.S)
            assert active_queue
            queue_html = active_queue.group()
            assert all(markers[key] in queue_html for key in ("urgent", "later", "no_deadline"))
            assert queue_html.index(markers["urgent"]) < queue_html.index(markers["later"])
            assert queue_html.index(markers["later"]) < queue_html.index(markers["no_deadline"])
            assert markers["paid"] not in queue_html and markers["failed"] not in queue_html
            assert markers["other"] not in active.text
            assert markers["paid"] in history.text and markers["failed"] in history.text
            assert markers["urgent"] not in history.text and markers["later"] not in history.text
            assert markers["no_deadline"] not in history.text
            assert markers["paid"] in filtered.text and markers["failed"] not in filtered.text
            assert 'data-metric="active_count">3</dd>' in filtered.text
            assert 'data-metric="urgent_count">1</dd>' in filtered.text
            assert 'data-metric="paid_today_count">1</dd>' in filtered.text
            amount_metric = re.search(r'data-metric="paid_today_amount">(.*?)</dd>', filtered.text, re.S)
            assert amount_metric
            assert '2500.00' in amount_metric.group(1).replace(' ', '').replace('\u00a0', '')
            assert 'RUB' in amount_metric.group(1)
            assert "name=\"bank_code\"" in filtered.text
            assert "name=\"payment_method\"" in filtered.text
            assert "name=\"status\"" in filtered.text
            for response in active_polls:
                assert "tradespace-partial:trader-deposits:active" in response.text
                assert all(markers[key] in response.text for key in ("urgent", "later", "no_deadline"))
                assert markers["paid"] not in response.text and markers["other"] not in response.text
                assert "Обработать" in response.text
            for response in history_polls:
                assert "tradespace-partial:trader-deposits:history" in response.text
                assert markers["paid"] in response.text and markers["failed"] in response.text
                assert markers["urgent"] not in response.text and markers["other"] not in response.text
                assert "Подробнее" in response.text
            assert markers["paid"] in filtered_poll.text
            assert markers["failed"] not in filtered_poll.text
            assert preview_active.status_code == preview_history.status_code == 200
            assert 'data-component="TraderScopePreview"' in preview_active.text
            assert 'data-component="TraderScopePreview"' in preview_history.text
            assert all(markers[key] in preview_active.text for key in ("urgent", "later", "no_deadline"))
            assert markers["paid"] not in preview_active.text
            assert markers["failed"] not in preview_active.text
            assert markers["other"] not in preview_active.text
            assert markers["paid"] in preview_history.text
            assert markers["failed"] in preview_history.text
            assert markers["urgent"] not in preview_history.text
            assert markers["no_deadline"] not in preview_history.text
            assert markers["other"] not in preview_history.text
            assert "Активные 3" in preview_active.text
            assert "История 2" in preview_history.text
            assert "Только просмотр" in preview_active.text
            assert f'/staff/cabinet/tradespace/operations/{row_ids[0]}' in preview_active.text
            assert "Обработать" not in preview_history.text
            assert "Подробнее" in preview_history.text
            assert all(response.status_code == 200 for response in preview_active_polls)
            assert all(response.status_code == 200 for response in preview_history_polls)
            for response in preview_active_polls:
                assert "tradespace-partial:trader-deposits:active" in response.text
                assert all(markers[key] in response.text for key in ("urgent", "later", "no_deadline"))
                assert markers["paid"] not in response.text and markers["other"] not in response.text
                assert "Обработать" in response.text
            for response in preview_history_polls:
                assert "tradespace-partial:trader-deposits:history" in response.text
                assert markers["paid"] in response.text and markers["failed"] in response.text
                assert markers["urgent"] not in response.text and markers["other"] not in response.text
                assert markers["no_deadline"] not in response.text
                assert "Подробнее" in response.text
                assert "Обработать" not in response.text
            assert tampered_preview.status_code == 200
            assert markers["urgent"] in tampered_preview.text
            assert markers["other"] not in tampered_preview.text
            assert clear_preview.status_code == 200
            assert missing_preview.status_code == 403
            assert invalid_preview.status_code == 403
            assert all(response.status_code == 403 for response in unauthorized_previews)
            assert all(markers["urgent"] not in response.text for response in unauthorized_previews)
            assert 'action="/staff/cabinet/trader"' in preview_history.text
            assert 'href="/staff/cabinet/trader?view=history"' in preview_history.text

            async with AsyncSession(engine) as verify:
                persisted_trader = await verify.get(User, trader_id)
                assert persisted_trader is not None
                assert persisted_trader.trader_balance == Decimal("9000.00")
                assert persisted_trader.trader_hold == Decimal("1250.00")
                persisted_rows = (
                    await verify.execute(select(Deposit).where(Deposit.id.in_(row_ids)))
                ).scalars().all()
                assert {row.status for row in persisted_rows} == {
                    DepositStatus.pending.value,
                    DepositStatus.paid.value,
                    DepositStatus.failed.value,
                }
        finally:
            await application_engine.dispose()
            await engine.dispose()

    asyncio.run(scenario())
