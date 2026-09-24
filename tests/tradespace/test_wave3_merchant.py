import asyncio
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import encrypt_secret, hash_password
from app.db.session import engine as application_engine
from app.main import app
from app.models import (
    ApiKey, Appeal, AppealMessage, AuditLog, Balance, Deposit, LedgerEntry,
    Merchant, MerchantRollingAccount,
    MerchantRollingTransfer, MerchantSettlement, MerchantWebhookSigningKey,
    Payout, Requisite, User, WebhookDeliveryAttempt, WebhookEvent,
)

PASSWORD = "tradespace-wave-three-password"


def _csrf(response: httpx.Response) -> str:
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', response.text)
    assert match, response.text[:1200]
    return match.group(1)


async def _login(client: httpx.AsyncClient, email: str) -> None:
    page = await client.get("/merchant/login")
    assert page.status_code == 200
    response = await client.post(
        "/merchant/login",
        data={"email": email, "password": PASSWORD, "csrf_token": _csrf(page)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/merchant/cabinet"


async def _fixture() -> dict:
    await application_engine.dispose(close=False)
    suffix = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    async with AsyncSession(application_engine, expire_on_commit=False) as db:
        owner = User(
            email=f"wave3-merchant-{suffix}@example.test",
            password_hash=hash_password(PASSWORD),
            role="merchant",
        )
        foreign_owner = User(
            email=f"wave3-foreign-{suffix}@example.test",
            password_hash=hash_password(PASSWORD),
            role="merchant",
        )
        trader = User(
            email=f"wave3-trader-{suffix}@example.test",
            password_hash=hash_password(PASSWORD),
            role="trader",
        )
        db.add_all([owner, foreign_owner, trader])
        await db.flush()
        merchant = Merchant(
            owner_id=owner.id,
            name=f"Wave 3 Merchant {suffix}",
            webhook_url="https://merchant.example.test/webhook",
            ip_whitelist=["203.0.113.10"],
            sandbox_mode=True,
        )
        foreign = Merchant(owner_id=foreign_owner.id, name=f"FOREIGN {suffix}")
        db.add_all([merchant, foreign])
        await db.flush()
        balance = Balance(
            merchant_id=merchant.id,
            currency="RUB",
            available=Decimal("76543.21"),
            frozen=Decimal("1234.56"),
        )
        requisite = Requisite(
            trader_id=trader.id,
            owner_name="Получатель Тест",
            full_name="Получатель Тест",
            method="sbp",
            value_encrypted=encrypt_secret(f"+7999123{suffix[-4:]}"),
            bank_code="sberbank",
            bank_name="СберБанк",
            enabled=True,
            status="active",
        )
        db.add_all([balance, requisite])
        await db.flush()
        paid = Deposit(
            merchant_id=merchant.id,
            external_id=f"MERCHANT-PAID-{suffix}",
            amount=Decimal("10000.00"),
            currency="RUB",
            method="sbp",
            status="paid",
            requisites_id=requisite.id,
            expires_at=now - timedelta(minutes=20),
            metadata_json={
                "merchant_fee_amount": "1500.00",
                "merchant_payable_amount": "8500.00",
                "platform_income_amount": "500.00",
                "secret": "FORBIDDEN-DEPOSIT-SECRET",
            },
        )
        pending = Deposit(
            merchant_id=merchant.id,
            external_id=f"MERCHANT-PENDING-{suffix}",
            amount=Decimal("2500.00"),
            currency="RUB",
            method="sbp",
            status="pending",
            requisites_id=requisite.id,
            expires_at=now + timedelta(minutes=8),
            metadata_json={"api_key": "FORBIDDEN-METADATA-KEY"},
        )
        foreign_deposit = Deposit(
            merchant_id=foreign.id,
            external_id=f"FOREIGN-DEPOSIT-{suffix}",
            amount=Decimal("99999.00"),
            currency="RUB",
            method="sbp",
            status="paid",
            requisites_id=requisite.id,
            expires_at=now - timedelta(minutes=1),
        )
        payout = Payout(
            merchant_id=merchant.id,
            external_id=f"MERCHANT-PAYOUT-{suffix}",
            amount=Decimal("321.00"),
            currency="RUB",
            method="sbp",
            status="completed",
            destination="masked destination",
        )
        db.add_all([paid, pending, foreign_deposit, payout])
        await db.flush()
        appeal = Appeal(
            operation_type="deposit",
            operation_id=paid.id,
            created_by=owner.id,
            status="opened",
            metadata_json={
                "merchant_id": str(merchant.id),
                "trader_id": str(trader.id),
                "amount_claimed": "10000.00",
                "requisite": f"+7999123{suffix[-4:]}",
                "recipient_bank": "СберБанк",
                "deadline_at": (now + timedelta(minutes=30)).isoformat(),
            },
        )
        db.add(appeal)
        await db.flush()
        db.add(AppealMessage(
            appeal_id=appeal.id,
            author_id=owner.id,
            message="Проверить зачисление по операции.",
        ))
        ledger = LedgerEntry(
            merchant_id=merchant.id,
            operation_id=paid.id,
            entry_type="credit",
            amount=Decimal("8500.00"),
            currency="RUB",
            description="successful deposit settle overflow",
            idempotency_key=f"wave3-ledger-{suffix}",
        )
        settlement = MerchantSettlement(
            merchant_id=merchant.id,
            requested_by_id=owner.id,
            processed_by_id=owner.id,
            amount_usdt=Decimal("100.00"),
            fee_usdt=Decimal("5.00"),
            rate_rub=Decimal("100.0000"),
            amount_rub=Decimal("10000.00"),
            fee_rub=Decimal("500.00"),
            total_debit_rub=Decimal("10500.00"),
            trc20_address="TJRabPrwbZy45sbavfcjinPJC18kjpRTv8",
            network="TRC20",
            tx_hash=f"tx-{suffix}",
            idempotency_key=f"wave3-settlement-{suffix}",
            status="completed",
            processed_at=now,
        )
        account = MerchantRollingAccount(
            merchant_id=merchant.id,
            principal_usdt=Decimal("100.000000"),
            recovered_usdt=Decimal("25.000000"),
            outstanding_usdt=Decimal("75.000000"),
            status="active",
        )
        db.add_all([ledger, settlement, account])
        await db.flush()
        transfer = MerchantRollingTransfer(
            merchant_id=merchant.id,
            rolling_account_id=None,
            sequence_no=2,
            amount_usdt=Decimal("40.000000"),
            recovered_usdt=Decimal("0.000000"),
            remaining_usdt=Decimal("0.000000"),
            network="TRC20",
            destination_address="TJRabPrwbZy45sbavfcjinPJC18kjpRTv8",
            tx_hash=f"rolling-{suffix}",
            status="pending_confirmation",
            source="registered",
            sent_at=now,
            created_by=trader.id,
            idempotency_key=f"wave3-transfer-{suffix}",
        )
        api_key = ApiKey(
            merchant_id=merchant.id,
            api_key=f"pk_test_{suffix}",
            secret_hash="FORBIDDEN-API-SECRET-HASH",
            mode="sandbox",
            is_active=True,
        )
        signing = MerchantWebhookSigningKey(
            merchant_id=merchant.id,
            key_id=f"whk_{suffix}",
            encrypted_secret="FORBIDDEN-WEBHOOK-SIGNING-SECRET",
            status="active",
            created_by=trader.id,
        )
        event = WebhookEvent(
            merchant_id=merchant.id,
            event_type="deposit.paid",
            payload={
                "operation_type": "deposit",
                "external_id": paid.external_id,
                "amount": "10000.00",
                "currency": "RUB",
                "status": "paid",
                "secret": "FORBIDDEN-PAYLOAD-SECRET",
            },
            status="retry",
            attempts=1,
            max_attempts=5,
            last_error="FORBIDDEN-REMOTE-ERROR-BODY",
            last_status_code=500,
            next_attempt_at=now + timedelta(minutes=1),
            correlation_id=uuid.uuid4().hex,
            signing_key_id=signing.id,
        )
        db.add_all([transfer, api_key, signing, event])
        await db.flush()
        attempt = WebhookDeliveryAttempt(
            webhook_event_id=event.id,
            attempt_no=1,
            status="retry",
            status_code=500,
            error="FORBIDDEN-ATTEMPT-ERROR",
            response_snippet="FORBIDDEN-RESPONSE-BODY",
        )
        db.add(attempt)
        await db.commit()
        return {
            "owner": owner, "foreign_owner": foreign_owner, "trader": trader,
            "merchant": merchant, "foreign": foreign, "balance": balance,
            "requisite": requisite, "deposits": [paid, pending, foreign_deposit],
            "payout": payout, "appeal": appeal, "settlement": settlement,
            "account": account, "transfer": transfer,
            "api_key": api_key, "signing": signing, "event": event, "attempt": attempt,
        }


async def _cleanup(data: dict) -> None:
    async with AsyncSession(application_engine) as db:
        await db.execute(delete(WebhookDeliveryAttempt).where(WebhookDeliveryAttempt.id == data["attempt"].id))
        await db.execute(delete(WebhookEvent).where(WebhookEvent.id == data["event"].id))
        await db.execute(delete(MerchantWebhookSigningKey).where(MerchantWebhookSigningKey.id == data["signing"].id))
        await db.execute(delete(ApiKey).where(ApiKey.id == data["api_key"].id))
        await db.execute(delete(MerchantRollingTransfer).where(MerchantRollingTransfer.id == data["transfer"].id))
        await db.execute(delete(MerchantRollingAccount).where(MerchantRollingAccount.id == data["account"].id))
        await db.execute(delete(MerchantSettlement).where(MerchantSettlement.id == data["settlement"].id))
        await db.execute(delete(LedgerEntry).where(LedgerEntry.merchant_id == data["merchant"].id))
        await db.execute(delete(AppealMessage).where(AppealMessage.appeal_id == data["appeal"].id))
        await db.execute(delete(Appeal).where(Appeal.id == data["appeal"].id))
        await db.execute(delete(Payout).where(Payout.id == data["payout"].id))
        await db.execute(delete(Deposit).where(Deposit.id.in_([row.id for row in data["deposits"]])))
        await db.execute(delete(Balance).where(Balance.merchant_id == data["merchant"].id))
        await db.execute(delete(Requisite).where(Requisite.id == data["requisite"].id))
        await db.execute(delete(Merchant).where(Merchant.id.in_([data["merchant"].id, data["foreign"].id])))
        await db.execute(delete(User).where(User.id.in_([data["owner"].id, data["foreign_owner"].id, data["trader"].id])))
        await db.commit()


def _quote():
    return {
        "rate_available": True,
        "rate_rub": Decimal("100.0000"),
        "fee_usdt": Decimal("5.00"),
        "fee_rub": Decimal("500.00"),
        "available_rub": Decimal("76543.21"),
        "pending_rub": Decimal("0.00"),
        "max_request_usdt": Decimal("760.43"),
        "rate_source": "rapira_live",
        "rate_updated_at": datetime.now(timezone.utc),
        "rate_stale": False,
    }


def test_wave3_merchant_populated_screens_scope_money_and_secret_boundaries(monkeypatch):
    async def fake_quote(*_args, **_kwargs):
        return _quote()

    monkeypatch.setattr(
        "app.presentation.tradespace.merchant.view_models.merchant_settlement_quote",
        fake_quote,
    )

    async def scenario():
        data = await _fixture()
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://localhost",
                headers={"X-TradeSpace-Preview": "wave3"},
            ) as client:
                await _login(client, data["owner"].email)

                async with AsyncSession(application_engine) as db:
                    before = {
                        "audit": await db.scalar(select(func.count(AuditLog.id))),
                        "ledger": await db.scalar(select(func.count(LedgerEntry.id)).where(LedgerEntry.merchant_id == data["merchant"].id)),
                        "settlements": await db.scalar(select(func.count(MerchantSettlement.id)).where(MerchantSettlement.merchant_id == data["merchant"].id)),
                        "balance": tuple((await db.execute(select(Balance.available, Balance.frozen).where(Balance.merchant_id == data["merchant"].id))).one()),
                        "deposit_statuses": tuple((await db.scalars(select(Deposit.status).where(Deposit.merchant_id == data["merchant"].id).order_by(Deposit.id))).all()),
                        "transfer_status": await db.scalar(select(MerchantRollingTransfer.status).where(MerchantRollingTransfer.id == data["transfer"].id)),
                    }

                overview = await client.get("/merchant/cabinet")
                assert overview.status_code == 200
                assert 'data-component="MerchantOverview"' in overview.text
                assert data["merchant"].name in overview.text
                assert "76543.21" in overview.text and "1234.56" in overview.text
                assert data["foreign"].name not in overview.text

                operations = await client.get("/merchant/cabinet/tradespace/operations")
                assert operations.status_code == 200
                assert data["deposits"][0].external_id in operations.text
                assert data["deposits"][1].external_id in operations.text
                assert data["deposits"][2].external_id not in operations.text
                assert data["payout"].external_id in operations.text
                filtered = await client.get(
                    "/merchant/cabinet/tradespace/operations",
                    params={"status": "paid", "query": data["deposits"][0].external_id},
                )
                assert data["deposits"][0].external_id in filtered.text
                assert data["deposits"][1].external_id not in filtered.text

                detail = await client.get(
                    f"/merchant/cabinet/tradespace/operations/{data['deposits'][0].id}"
                )
                assert detail.status_code == 200
                assert 'data-component="MerchantOperationDetail"' in detail.text
                assert "1500.00" in detail.text and "8500.00" in detail.text
                assert data["deposits"][0].external_id in detail.text
                foreign = await client.get(
                    f"/merchant/cabinet/tradespace/operations/{data['deposits'][2].id}"
                )
                assert foreign.status_code == 404

                finance = await client.get("/merchant/cabinet/tradespace/finance")
                assert "Доступные средства" in finance.text and "Зарезервировано" in finance.text and "Rolling" in finance.text
                assert "75.000000" in finance.text
                assert f'action="/merchant/cabinet/rolling/transfers/{data["transfer"].id}/confirm"' in finance.text
                assert f'action="/merchant/cabinet/rolling/transfers/{data["transfer"].id}/dispute"' in finance.text

                settlements = await client.get("/merchant/cabinet/tradespace/settlements")
                assert settlements.status_code == 200
                assert "Запросы расчёта" in settlements.text
                assert "10500.00" in settlements.text
                assert 'action="/merchant/cabinet/settlements/request"' in settlements.text
                assert "760.43" in settlements.text

                integration = await client.get("/merchant/cabinet/tradespace/integration")
                assert integration.status_code == 200
                assert data["api_key"].api_key in integration.text
                assert data["signing"].key_id in integration.text
                assert "deposit.paid" in integration.text
                assert "HTTP 500" in integration.text
                assert f'action="/merchant/cabinet/merchants/{data["merchant"].id}/integration"' in integration.text

                disputes = await client.get("/merchant/cabinet/tradespace/disputes")
                assert disputes.status_code == 200
                assert str(data["appeal"].id)[:8] in disputes.text
                assert "Проверить зачисление по операции." in disputes.text

                analytics = await client.get("/merchant/cabinet/tradespace/analytics")
                assert analytics.status_code == 200
                assert "10000.00" in analytics.text
                assert "8500.00" in analytics.text
                assert "1500.00" in analytics.text

                account = await client.get("/merchant/cabinet/tradespace/security")
                assert account.status_code == 200
                assert "<h1>Аккаунт</h1>" in account.text
                assert 'action="/merchant/cabinet/security/password"' in account.text
                assert 'data-component="MobileNavigation"' in account.text

                combined = "\n".join([
                    overview.text, operations.text, detail.text, finance.text,
                    settlements.text, integration.text, disputes.text, analytics.text,
                ])
                for forbidden in (
                    "FORBIDDEN-DEPOSIT-SECRET", "FORBIDDEN-METADATA-KEY",
                    "FORBIDDEN-API-SECRET-HASH", "FORBIDDEN-WEBHOOK-SIGNING-SECRET",
                    "FORBIDDEN-PAYLOAD-SECRET", "FORBIDDEN-REMOTE-ERROR-BODY",
                    "FORBIDDEN-ATTEMPT-ERROR", "FORBIDDEN-RESPONSE-BODY",
                ):
                    assert forbidden not in combined

                async with AsyncSession(application_engine) as db:
                    after = {
                        "audit": await db.scalar(select(func.count(AuditLog.id))),
                        "ledger": await db.scalar(select(func.count(LedgerEntry.id)).where(LedgerEntry.merchant_id == data["merchant"].id)),
                        "settlements": await db.scalar(select(func.count(MerchantSettlement.id)).where(MerchantSettlement.merchant_id == data["merchant"].id)),
                        "balance": tuple((await db.execute(select(Balance.available, Balance.frozen).where(Balance.merchant_id == data["merchant"].id))).one()),
                        "deposit_statuses": tuple((await db.scalars(select(Deposit.status).where(Deposit.merchant_id == data["merchant"].id).order_by(Deposit.id))).all()),
                        "transfer_status": await db.scalar(select(MerchantRollingTransfer.status).where(MerchantRollingTransfer.id == data["transfer"].id)),
                    }
                assert after == before
        finally:
            await _cleanup(data)

    asyncio.run(scenario())


def test_wave3_settlement_action_reuses_csrf_and_validation(monkeypatch):
    async def fake_quote(*_args, **_kwargs):
        return _quote()

    monkeypatch.setattr(
        "app.presentation.tradespace.merchant.view_models.merchant_settlement_quote",
        fake_quote,
    )

    async def scenario():
        data = await _fixture()
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://localhost",
                headers={"X-TradeSpace-Preview": "wave3"},
            ) as client:
                await _login(client, data["owner"].email)
                missing = await client.post(
                    "/merchant/cabinet/settlements/request",
                    data={
                        "amount_usdt": "0",
                        "trc20_address": "TJRabPrwbZy45sbavfcjinPJC18kjpRTv8",
                        "idempotency_key": f"missing-{uuid.uuid4().hex}",
                    },
                    follow_redirects=False,
                )
                assert missing.status_code == 403
                page = await client.get("/merchant/cabinet/tradespace/settlements")
                invalid = await client.post(
                    "/merchant/cabinet/settlements/request",
                    data={
                        "csrf_token": _csrf(page),
                        "amount_usdt": "0",
                        "trc20_address": "TJRabPrwbZy45sbavfcjinPJC18kjpRTv8",
                        "idempotency_key": f"invalid-{uuid.uuid4().hex}",
                    },
                    follow_redirects=False,
                )
                assert invalid.status_code == 303
                async with AsyncSession(application_engine) as db:
                    count = await db.scalar(select(func.count(MerchantSettlement.id)).where(
                        MerchantSettlement.merchant_id == data["merchant"].id
                    ))
                assert count == 1
        finally:
            await _cleanup(data)

    asyncio.run(scenario())


def test_wave3_empty_state_unauthorized_and_role_isolation(monkeypatch):
    async def fake_quote(*_args, **_kwargs):
        return _quote()

    monkeypatch.setattr(
        "app.presentation.tradespace.merchant.view_models.merchant_settlement_quote",
        fake_quote,
    )

    async def scenario():
        await application_engine.dispose(close=False)
        suffix = uuid.uuid4().hex
        async with AsyncSession(application_engine, expire_on_commit=False) as db:
            owner = User(email=f"wave3-empty-{suffix}@example.test", password_hash=hash_password(PASSWORD), role="merchant")
            trader = User(email=f"wave3-wrong-{suffix}@example.test", password_hash=hash_password(PASSWORD), role="trader")
            db.add_all([owner, trader])
            await db.flush()
            merchant = Merchant(owner_id=owner.id, name="Empty Merchant")
            db.add(merchant)
            await db.commit()
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="https://localhost", headers={"X-TradeSpace-Preview": "wave3"}) as client:
                unauthorized = await client.get("/merchant/cabinet", follow_redirects=False)
                assert unauthorized.status_code == 303
                assert unauthorized.headers["location"] == "/merchant/login"
                await _login(client, owner.email)
                overview = await client.get("/merchant/cabinet")
                assert "Операций ещё нет" in overview.text
                assert 'data-component="MobileNavigation"' in overview.text
                operations = await client.get("/merchant/cabinet/tradespace/operations")
                assert "Подходящих операций не найдено" in operations.text
                finance = await client.get("/merchant/cabinet/tradespace/finance")
                assert "Rolling не подключён" in finance.text
                integration = await client.get("/merchant/cabinet/tradespace/integration")
                assert "API keys не выпущены" in integration.text
                disputes = await client.get("/merchant/cabinet/tradespace/disputes")
                assert "Споров нет" in disputes.text

            async with httpx.AsyncClient(transport=transport, base_url="https://localhost", headers={"X-TradeSpace-Preview": "wave3"}) as client:
                page = await client.get("/trader/login")
                response = await client.post("/trader/login", data={
                    "email": trader.email, "password": PASSWORD, "csrf_token": _csrf(page)
                }, follow_redirects=False)
                assert response.status_code == 303
                forbidden = await client.get("/trader/cabinet/tradespace/settlements")
                assert forbidden.status_code == 403
        finally:
            async with AsyncSession(application_engine) as db:
                await db.execute(delete(Merchant).where(Merchant.id == merchant.id))
                await db.execute(delete(User).where(User.id.in_([owner.id, trader.id])))
                await db.commit()

    asyncio.run(scenario())
