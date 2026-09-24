import asyncio
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import AppealStatus, DepositStatus, Role
from app.core.security import encrypt_secret, hash_password
from app.db.session import engine as application_engine
from app.main import app
from app.models import (
    Appeal,
    AppealMessage,
    AuditLog,
    Deposit,
    Merchant,
    Requisite,
    SmsMessage,
    TraderLedgerEntry,
    User,
)


PASSWORD = "tradespace-wave-two-password"


def _csrf(response: httpx.Response) -> str:
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', response.text)
    assert match, response.text[:1200]
    return match.group(1)


async def _login(client: httpx.AsyncClient, user: User) -> None:
    page = await client.get("/trader/login")
    assert page.status_code == 200
    response = await client.post(
        "/trader/login",
        data={
            "email": user.email,
            "password": PASSWORD,
            "csrf_token": _csrf(page),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/trader/cabinet"


async def _fixture() -> dict:
    await application_engine.dispose(close=False)
    suffix = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    async with AsyncSession(application_engine, expire_on_commit=False) as db:
        trader = User(
            email=f"wave2-trader-{suffix}@example.test",
            password_hash=hash_password(PASSWORD),
            role=Role.trader.value,
            trader_balance=Decimal("10000.00"),
            trader_hold=Decimal("1000.00"),
            trader_traffic_status="active",
        )
        other = User(
            email=f"wave2-other-{suffix}@example.test",
            password_hash=hash_password(PASSWORD),
            role=Role.trader.value,
        )
        merchant_owner = User(
            email=f"wave2-merchant-{suffix}@example.test",
            password_hash=hash_password(PASSWORD),
            role=Role.merchant.value,
        )
        db.add_all([trader, other, merchant_owner])
        await db.flush()
        merchant = Merchant(owner_id=merchant_owner.id, name=f"PRIVATE MERCHANT {suffix}")
        db.add(merchant)
        await db.flush()
        own_req = Requisite(
            trader_id=trader.id,
            owner_name="Иван Трейдер",
            full_name="Иван Трейдер",
            method="sbp",
            value_encrypted=encrypt_secret(f"+7999000{suffix[-4:]}"),
            bank_code="sberbank",
            bank_name="СберБанк",
            enabled=True,
            status="active",
            daily_limit=Decimal("500000.00"),
            operation_limit=50,
            simultaneous_limit=2,
            min_check=Decimal("100.00"),
            max_check=Decimal("150000.00"),
        )
        disabled_req = Requisite(
            trader_id=trader.id,
            owner_name="Иван Трейдер",
            method="c2c",
            value_encrypted=encrypt_secret(f"411111111111{suffix[-4:]}"),
            bank_code="tbank",
            bank_name="Т-Банк",
            enabled=False,
            status="disabled",
            daily_limit=Decimal("100000.00"),
            operation_limit=10,
            simultaneous_limit=1,
            min_check=Decimal("500.00"),
            max_check=Decimal("50000.00"),
        )
        other_req = Requisite(
            trader_id=other.id,
            owner_name="Другой трейдер",
            method="sbp",
            value_encrypted=encrypt_secret(f"+7888000{suffix[-4:]}"),
            bank_code="sberbank",
            bank_name="СберБанк",
        )
        db.add_all([own_req, disabled_req, other_req])
        await db.flush()

        pending = Deposit(
            merchant_id=merchant.id,
            external_id=f"W2-PENDING-{suffix}",
            amount=Decimal("1000.00"),
            currency="RUB",
            method="sbp",
            status=DepositStatus.pending.value,
            requisites_id=own_req.id,
            expires_at=now + timedelta(minutes=4),
            metadata_json={
                "trader_hold_amount": "950.00",
                "trader_settlement_amount": "950.00",
                "trader_profit_amount": "50.00",
                "trader_hold_status": "active",
                "webhook_secret": "FORBIDDEN-WEBHOOK-SECRET",
                "client_ip": "203.0.113.200",
            },
        )
        paid = Deposit(
            merchant_id=merchant.id,
            external_id=f"W2-PAID-{suffix}",
            amount=Decimal("2500.00"),
            currency="RUB",
            method="c2c",
            status=DepositStatus.paid.value,
            requisites_id=disabled_req.id,
            expires_at=now - timedelta(minutes=30),
            metadata_json={
                "trader_hold_amount": "2375.00",
                "trader_settlement_amount": "2375.00",
                "trader_profit_amount": "125.00",
                "trader_hold_status": "settled",
                "api_key": "FORBIDDEN-API-KEY",
            },
        )
        paid_clean = Deposit(
            merchant_id=merchant.id,
            external_id=f"W2-PAID-CLEAN-{suffix}",
            amount=Decimal("1800.00"),
            currency="RUB",
            method="sbp",
            status=DepositStatus.paid.value,
            requisites_id=own_req.id,
            expires_at=now - timedelta(minutes=20),
            metadata_json={
                "trader_hold_amount": "1710.00",
                "trader_settlement_amount": "1710.00",
                "trader_profit_amount": "90.00",
                "trader_hold_status": "settled",
            },
        )
        failed = Deposit(
            merchant_id=merchant.id,
            external_id=f"W2-FAILED-{suffix}",
            amount=Decimal("700.00"),
            currency="RUB",
            method="sbp",
            status=DepositStatus.failed.value,
            requisites_id=own_req.id,
            expires_at=now - timedelta(hours=1),
            metadata_json={"failure_reason": "trader_timeout", "token": "FORBIDDEN-TOKEN"},
        )
        other_deposit = Deposit(
            merchant_id=merchant.id,
            external_id=f"W2-OTHER-{suffix}",
            amount=Decimal("9999.00"),
            currency="RUB",
            method="sbp",
            status=DepositStatus.pending.value,
            requisites_id=other_req.id,
            expires_at=now + timedelta(minutes=10),
        )
        db.add_all([pending, paid, paid_clean, failed, other_deposit])
        await db.flush()
        appeal = Appeal(
            operation_type="deposit",
            operation_id=paid.id,
            created_by=merchant_owner.id,
            status=AppealStatus.opened.value,
            metadata_json={
                "trader_id": str(trader.id),
                "amount_claimed": "2500.00",
                "requisite": f"411111111111{suffix[-4:]}",
                "recipient_bank": "Т-Банк",
                "deadline_at": (now + timedelta(minutes=20)).isoformat(),
                "previous_deposit_status": DepositStatus.paid.value,
            },
        )
        db.add(appeal)
        await db.flush()
        db.add_all(
            [
                AppealMessage(
                    appeal_id=appeal.id,
                    author_id=merchant_owner.id,
                    message="Платёж требует проверки.",
                ),
                SmsMessage(
                    provider_message_id=f"w2-sms-{suffix}",
                    sender="BANK",
                    body="Sensitive original SMS body must stay hidden",
                    parsed_amount=Decimal("1000.00"),
                    message_hash=uuid.uuid4().hex,
                    linked_deposit_id=pending.id,
                    processed=True,
                ),
                TraderLedgerEntry(
                    trader_id=trader.id,
                    operation_id=pending.id,
                    entry_type="hold",
                    amount=Decimal("950.00"),
                    balance_after=Decimal("10000.00"),
                    hold_after=Decimal("1000.00"),
                    currency="RUB",
                    description="deposit requisite reserved",
                    idempotency_key=f"wave2-ledger-{suffix}",
                ),
            ]
        )
        await db.commit()
        return {
            "users": [trader, other, merchant_owner],
            "merchant": merchant,
            "requisites": [own_req, disabled_req, other_req],
            "deposits": [pending, paid, paid_clean, failed, other_deposit],
            "appeal": appeal,
            "markers": {
                "pending": pending.external_id,
                "pending_id": str(pending.id),
                "paid": paid.external_id,
                "paid_id": str(paid.id),
                "paid_clean": paid_clean.external_id,
                "paid_clean_id": str(paid_clean.id),
                "failed": failed.external_id,
                "failed_id": str(failed.id),
                "other": other_deposit.external_id,
                "other_id": str(other_deposit.id),
                "own_req": f"+7999000{suffix[-4:]}",
                "disabled_req": f"411111111111{suffix[-4:]}",
                "other_req": f"+7888000{suffix[-4:]}",
                "merchant": merchant.name,
            },
        }


async def _cleanup(data: dict) -> None:
    async with AsyncSession(application_engine) as db:
        appeal_ids = [data["appeal"].id]
        deposit_ids = [row.id for row in data["deposits"]]
        user_ids = [row.id for row in data["users"]]
        await db.execute(delete(AppealMessage).where(AppealMessage.appeal_id.in_(appeal_ids)))
        await db.execute(delete(Appeal).where(Appeal.id.in_(appeal_ids)))
        await db.execute(delete(SmsMessage).where(SmsMessage.linked_deposit_id.in_(deposit_ids)))
        await db.execute(delete(TraderLedgerEntry).where(TraderLedgerEntry.trader_id.in_(user_ids)))
        await db.execute(delete(Deposit).where(Deposit.id.in_(deposit_ids)))
        await db.execute(delete(Requisite).where(Requisite.id.in_([row.id for row in data["requisites"]])))
        await db.execute(delete(Merchant).where(Merchant.id == data["merchant"].id))
        await db.execute(delete(User).where(User.id.in_(user_ids)))
        await db.commit()


def test_wave2_trader_workbench_sections_scope_and_read_only_contract():
    async def scenario():
        await application_engine.dispose(close=False)
        data = await _fixture()
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://localhost",
                headers={"X-TradeSpace-Preview": "wave2"},
            ) as client:
                trader = data["users"][0]
                await _login(client, trader)
                markers = data["markers"]

                async with AsyncSession(application_engine) as db:
                    before = {
                        "audit": await db.scalar(select(func.count(AuditLog.id))),
                        "appeals": await db.scalar(select(func.count(Appeal.id))),
                        "ledger": await db.scalar(select(func.count(TraderLedgerEntry.id))),
                        "statuses": tuple(
                            (
                                await db.execute(
                                    select(Deposit.status)
                                    .where(Deposit.id.in_([row.id for row in data["deposits"]]))
                                    .order_by(Deposit.id)
                                )
                            ).scalars().all()
                        ),
                    }

                work = await client.get("/trader/cabinet")
                assert work.status_code == 200
                assert 'data-component="TraderWorkbench"' in work.text
                assert markers["pending_id"][:8] in work.text
                assert markers["failed_id"][:8] in work.text
                assert markers["other_id"][:8] not in work.text
                assert markers["merchant"] not in work.text
                assert "FORBIDDEN-WEBHOOK-SECRET" not in work.text
                assert "203.0.113.200" not in work.text
                assert "Sensitive original SMS body" not in work.text
                assert "Доступно" in work.text and "9000.00" in work.text
                assert "Подтвердить оплату" not in work.text

                detail = await client.get(
                    f"/trader/cabinet/tradespace/operations/{data['deposits'][0].id}"
                )
                assert detail.status_code == 200
                assert "Подтвердить оплату" in detail.text
                assert f'action="/trader/cabinet/deposits/{data["deposits"][0].id}/confirm"' in detail.text
                assert "950.00" in detail.text and "50.00" in detail.text
                assert "Decline" not in detail.text and "Отклонить операцию" not in detail.text
                assert "FORBIDDEN-WEBHOOK-SECRET" not in detail.text
                assert "Sensitive original SMS body" not in detail.text

                foreign_detail = await client.get(
                    f"/trader/cabinet/tradespace/operations/{data['deposits'][4].id}"
                )
                assert foreign_detail.status_code == 404
                invalid_detail = await client.get(
                    "/trader/cabinet/tradespace/operations/not-a-uuid"
                )
                assert invalid_detail.status_code == 404

                history = await client.get("/trader/cabinet/tradespace/history")
                assert history.status_code == 200
                assert markers["paid_clean_id"][:8] in history.text and markers["failed_id"][:8] in history.text
                assert markers["pending_id"][:8] not in history.text and markers["other_id"][:8] not in history.text
                assert markers["paid_id"][:8] not in history.text
                filtered = await client.get(
                    "/trader/cabinet/tradespace/history",
                    params={"status": "paid"},
                )
                assert markers["paid_clean_id"][:8] in filtered.text and markers["failed_id"][:8] not in filtered.text
                assert markers["paid_id"][:8] not in filtered.text

                requisites = await client.get("/trader/cabinet/tradespace/requisites")
                assert requisites.status_code == 200
                assert markers["own_req"] in requisites.text
                assert markers["disabled_req"] in requisites.text
                assert markers["other_req"] not in requisites.text
                assert f'action="/trader/cabinet/requisites/{data["requisites"][0].id}/toggle"' in requisites.text
                assert 'action="/trader/cabinet/requisites/create"' in requisites.text
                assert requisites.text.count('name="csrf_token"') >= 3

                finance = await client.get("/trader/cabinet/tradespace/finance")
                assert finance.status_code == 200
                assert "История движений" in finance.text
                assert "Резерв" in finance.text
                assert "10000.00" in finance.text and "1000.00" in finance.text

                disputes = await client.get("/trader/cabinet/tradespace/disputes")
                assert disputes.status_code == 200
                assert str(data["appeal"].id)[:8] in disputes.text
                assert "Платёж требует проверки." in disputes.text
                assert f'action="/trader/cabinet/appeals/{data["appeal"].id}/trader/accept"' in disputes.text
                assert markers["merchant"] not in disputes.text

                analytics = await client.get("/trader/cabinet/tradespace/analytics")
                assert analytics.status_code == 200
                assert "Закрыто операций" in analytics.text
                assert "4300.00" in analytics.text
                assert "215.00" in analytics.text

                account = await client.get("/trader/cabinet/tradespace/security")
                assert account.status_code == 200
                assert "<h1>Аккаунт</h1>" in account.text
                assert 'action="/trader/cabinet/security/password"' in account.text

                alias = await client.get("/trader/cabinet", params={"section": "appeals"})
                assert alias.status_code == 200 and "<h1>Споры</h1>" in alias.text

                csrf_denied = await client.post(
                    f"/trader/cabinet/requisites/{data['requisites'][0].id}/toggle",
                    follow_redirects=False,
                )
                assert csrf_denied.status_code == 403

                async with AsyncSession(application_engine) as db:
                    after = {
                        "audit": await db.scalar(select(func.count(AuditLog.id))),
                        "appeals": await db.scalar(select(func.count(Appeal.id))),
                        "ledger": await db.scalar(select(func.count(TraderLedgerEntry.id))),
                        "statuses": tuple(
                            (
                                await db.execute(
                                    select(Deposit.status)
                                    .where(Deposit.id.in_([row.id for row in data["deposits"]]))
                                    .order_by(Deposit.id)
                                )
                            ).scalars().all()
                        ),
                    }
                assert after == before
        finally:
            await _cleanup(data)

    asyncio.run(scenario())


def test_wave2_empty_state_and_unauthorized_realm():
    async def scenario():
        await application_engine.dispose(close=False)
        suffix = uuid.uuid4().hex
        trader = User(
            email=f"wave2-empty-{suffix}@example.test",
            password_hash=hash_password(PASSWORD),
            role=Role.trader.value,
        )
        merchant = User(
            email=f"wave2-wrong-realm-{suffix}@example.test",
            password_hash=hash_password(PASSWORD),
            role=Role.merchant.value,
        )
        async with AsyncSession(application_engine, expire_on_commit=False) as db:
            db.add_all([trader, merchant])
            await db.commit()
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://localhost",
                headers={"X-TradeSpace-Preview": "wave2"},
            ) as client:
                unauthorized = await client.get("/trader/cabinet", follow_redirects=False)
                assert unauthorized.status_code == 303
                assert unauthorized.headers["location"] == "/trader/login"
                await _login(client, trader)
                work = await client.get("/trader/cabinet")
                assert "Очередь пуста" in work.text
                assert 'data-component="MobileNavigation"' in work.text
                assert 'name="viewport"' in work.text
                requisites = await client.get("/trader/cabinet/tradespace/requisites")
                assert "Реквизитов нет" in requisites.text
                disputes = await client.get("/trader/cabinet/tradespace/disputes")
                assert "Споров нет" in disputes.text

            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://localhost",
                headers={"X-TradeSpace-Preview": "wave2"},
            ) as client:
                page = await client.get("/merchant/login")
                response = await client.post(
                    "/merchant/login",
                    data={
                        "email": merchant.email,
                        "password": PASSWORD,
                        "csrf_token": _csrf(page),
                    },
                    follow_redirects=False,
                )
                assert response.status_code == 303
                wrong_realm = await client.get("/trader/cabinet", follow_redirects=False)
                assert wrong_realm.status_code in {303, 403}
        finally:
            async with AsyncSession(application_engine) as db:
                await db.execute(delete(User).where(User.id.in_([trader.id, merchant.id])))
                await db.commit()

    asyncio.run(scenario())






