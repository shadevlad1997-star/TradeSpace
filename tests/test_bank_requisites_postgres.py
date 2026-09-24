import asyncio
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import settings
from app.core.enums import DepositStatus, Role
from app.core.merchant_hmac import canonical_request_v2, sign_request_v2
from app.core.security import encrypt_secret, hash_password
from app.db.session import engine as application_engine
from app.main import app
from app.models import ApiKey, Deposit, Merchant, Requisite, User


NEW_BANKS = (
    ('cifra_bank', 'Цифра банк'),
    ('mts_money_exi_bank', 'МТС Деньги (ЭКСИ-Банк)'),
)


def _database_url() -> str:
    value = os.getenv('TEST_DATABASE_URL', '')
    if not value:
        pytest.skip('TEST_DATABASE_URL is required for bank requisite tests')
    if 'postgresql' not in value:
        pytest.fail('Bank requisite integration tests require PostgreSQL')
    if value.startswith('postgresql://'):
        return value.replace('postgresql://', 'postgresql+asyncpg://', 1)
    return value


def _csrf(response: httpx.Response) -> str:
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', response.text)
    assert match, response.text[:500]
    return match.group(1)


def _merchant_get_headers(
    *,
    api_key: str,
    secret: str,
    path: str,
    nonce: str,
) -> dict[str, str]:
    timestamp = str(int(time.time()))
    canonical = canonical_request_v2(
        timestamp=timestamp,
        nonce=nonce,
        method='GET',
        path=path,
        query='',
        content_type='application/json',
        body=b'',
    )
    return {
        'X-API-Key': api_key,
        'X-Signature-Version': '2',
        'X-Timestamp': timestamp,
        'X-Nonce': nonce,
        'X-Signature': sign_request_v2(secret, canonical),
        'Content-Type': 'application/json',
    }


def _requisite_form(
    *,
    csrf: str,
    code: str,
    value: str,
    full_name: str,
    daily_limit: str,
) -> dict[str, str]:
    return {
        'csrf_token': csrf,
        'method': 'sbp',
        'value': value,
        'bank_code': code,
        'operator_code': '',
        'bank_name': '',
        'full_name': full_name,
        'automation_id': '',
        'last4': value[-4:],
        'daily_limit': daily_limit,
        'operation_limit': '50',
        'request_count': '10',
        'timeframe': 'час',
        'success_delay_minutes': '0',
        'simultaneous_limit': '2',
        'min_check': '100',
        'max_check': '150000',
        'status': 'active',
    }


def test_new_banks_work_in_trader_create_edit_and_merchant_api():
    async def scenario():
        await application_engine.dispose(close=False)
        engine = create_async_engine(_database_url())
        suffix = uuid.uuid4().hex
        trader_password = f'Bank-test-{suffix}'
        api_key_value = f'bank_test_{suffix}'
        api_secret = f'bank-test-secret-{suffix}'
        test_names = {
            code: f'Bank requisite {code} {suffix}'
            for code, _ in NEW_BANKS
        }
        requisite_ids: dict[str, uuid.UUID] = {}
        external_ids: dict[str, str] = {}

        redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text('TRUNCATE TABLE users RESTART IDENTITY CASCADE')
                )
            await redis.flushdb()

            async with AsyncSession(engine, expire_on_commit=False) as db:
                trader = User(
                    email=f'bank-trader-{suffix}@example.test',
                    password_hash=hash_password(trader_password),
                    role=Role.operator.value,
                    trader_balance=Decimal('1000000.00'),
                )
                merchant_owner = User(
                    email=f'bank-merchant-{suffix}@example.test',
                    password_hash=hash_password(f'Merchant-{suffix}'),
                    role=Role.merchant.value,
                )
                db.add_all([trader, merchant_owner])
                await db.flush()
                merchant = Merchant(
                    owner_id=merchant_owner.id,
                    name=f'Bank API merchant {suffix}',
                    sandbox_mode=True,
                )
                db.add(merchant)
                await db.flush()
                db.add(
                    ApiKey(
                        merchant_id=merchant.id,
                        api_key=api_key_value,
                        secret_hash=encrypt_secret(api_secret),
                        mode='sandbox',
                        is_active=True,
                    )
                )
                await db.commit()

            transport = httpx.ASGITransport(
                app=app,
                raise_app_exceptions=False,
            )
            async with httpx.AsyncClient(
                transport=transport,
                base_url='https://localhost',
                follow_redirects=False,
            ) as trader_client:
                login_page = await trader_client.get('/trader/login')
                assert login_page.status_code == 200
                login = await trader_client.post(
                    '/trader/login',
                    data={
                        'csrf_token': _csrf(login_page),
                        'email': f'bank-trader-{suffix}@example.test',
                        'password': trader_password,
                    },
                )
                assert login.status_code == 303, login.text

                cabinet = await trader_client.get('/trader/cabinet/tradespace/requisites')
                assert cabinet.status_code == 200, cabinet.text
                csrf = _csrf(cabinet)
                assert 'Цифра банк' in cabinet.text
                assert 'МТС Деньги (ЭКСИ-Банк)' in cabinet.text

                for index, (code, _) in enumerate(NEW_BANKS, 1):
                    created = await trader_client.post(
                        '/trader/cabinet/requisites/create',
                        data=_requisite_form(
                            csrf=csrf,
                            code=code,
                            value=f'+7999000000{index}',
                            full_name=test_names[code],
                            daily_limit='500000',
                        ),
                    )
                    assert created.status_code == 303, created.text

                async with AsyncSession(engine, expire_on_commit=False) as db:
                    rows = (
                        await db.execute(
                            select(Requisite).where(
                                Requisite.full_name.in_(test_names.values())
                            )
                        )
                    ).scalars().all()
                    assert len(rows) == 2
                    by_code = {row.bank_code: row for row in rows}
                    for code, name in NEW_BANKS:
                        row = by_code[code]
                        assert row.bank_name == name
                        requisite_ids[code] = row.id

                for index, (code, _) in enumerate(NEW_BANKS, 1):
                    edited = await trader_client.post(
                        f'/trader/cabinet/requisites/{requisite_ids[code]}/edit',
                        data=_requisite_form(
                            csrf=csrf,
                            code=code,
                            value=f'+7999111000{index}',
                            full_name=test_names[code],
                            daily_limit='600000',
                        ),
                    )
                    assert edited.status_code == 303, edited.text

                updated_cabinet = await trader_client.get('/trader/cabinet/tradespace/requisites')
                assert updated_cabinet.status_code == 200
                for _, name in NEW_BANKS:
                    assert name in updated_cabinet.text

                async with AsyncSession(engine, expire_on_commit=False) as db:
                    rows = (
                        await db.execute(
                            select(Requisite).where(
                                Requisite.id.in_(requisite_ids.values())
                            )
                        )
                    ).scalars().all()
                    assert len(rows) == 2
                    for row in rows:
                        assert row.daily_limit == Decimal('600000.00')
                        external_id = f'bank-api-{row.bank_code}-{suffix}'
                        external_ids[row.bank_code] = external_id
                        db.add(
                            Deposit(
                                merchant_id=merchant.id,
                                external_id=external_id,
                                idempotency_key=external_id,
                                amount=Decimal('1000.00'),
                                currency='RUB',
                                method='sbp',
                                status=DepositStatus.pending.value,
                                requisites_id=row.id,
                                expires_at=(
                                    datetime.now(timezone.utc)
                                    + timedelta(minutes=15)
                                ),
                                metadata_json={},
                            )
                        )
                    await db.commit()

                async with httpx.AsyncClient(
                    transport=transport,
                    base_url='https://localhost',
                ) as merchant_client:
                    for index, (code, name) in enumerate(NEW_BANKS, 1):
                        path = (
                            '/api/v1/merchant/deposits/'
                            f'{external_ids[code]}'
                        )
                        response = await merchant_client.get(
                            path,
                            headers=_merchant_get_headers(
                                api_key=api_key_value,
                                secret=api_secret,
                                path=path,
                                nonce=f'bank-get-{index}-{suffix}',
                            ),
                        )
                        assert response.status_code == 200, response.text
                        details = response.json()['payment_details']
                        assert details['bank_code'] == code
                        assert details['bank_name'] == name
                        assert details['bank'] == name
                        assert details['provider_name'] == name

                for code, _ in NEW_BANKS:
                    deleted = await trader_client.post(
                        f'/trader/cabinet/requisites/{requisite_ids[code]}/delete',
                        data={'csrf_token': csrf},
                    )
                    assert deleted.status_code == 303, deleted.text

                async with AsyncSession(engine) as db:
                    deleted_rows = (
                        await db.execute(
                            select(Requisite).where(
                                Requisite.id.in_(requisite_ids.values())
                            )
                        )
                    ).scalars().all()
                    assert len(deleted_rows) == 2
                    assert all(row.status == 'deleted' for row in deleted_rows)
                    assert all(row.enabled is False for row in deleted_rows)
        finally:
            await redis.flushdb()
            await redis.aclose()
            async with engine.begin() as connection:
                await connection.execute(
                    text('TRUNCATE TABLE users RESTART IDENTITY CASCADE')
                )
            await engine.dispose()
            await application_engine.dispose(close=False)

    asyncio.run(scenario())
