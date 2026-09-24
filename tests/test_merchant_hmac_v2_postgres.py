import asyncio
import json
import os
import time
import uuid
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import app.api.deps as auth_deps
from app.core.config import settings
from app.core.enums import Role
from app.core.merchant_hmac import (
    canonical_request_v2,
    sign_request_v2,
    verify_request_v2,
)
from app.core.security import encrypt_secret, hash_password, sign_hmac
from app.db.session import engine as application_engine
from app.main import app
from app.models import ApiKey, Balance, LedgerEntry, Merchant, Payout, User


API_PATH = '/api/v1/merchant/payouts'
CONTENT_TYPE = 'application/json'


def _database_url() -> str:
    value = os.getenv('TEST_DATABASE_URL', '')
    if not value:
        pytest.skip('TEST_DATABASE_URL is required for HMAC v2 tests')
    if value.startswith('postgresql://'):
        return value.replace('postgresql://', 'postgresql+asyncpg://', 1)
    return value


def _signed_headers(
    *,
    api_key: str,
    secret: str,
    body: bytes,
    nonce: str,
    idempotency_key: str | None,
    timestamp: int | None = None,
    method: str = 'POST',
    signed_path: str = API_PATH,
    signed_query: str = '',
    signed_content_type: str = CONTENT_TYPE,
    signed_body: bytes | None = None,
) -> dict[str, str]:
    timestamp_text = str(timestamp if timestamp is not None else int(time.time()))
    canonical = canonical_request_v2(
        timestamp=timestamp_text,
        nonce=nonce,
        method=method,
        path=signed_path,
        query=signed_query,
        content_type=signed_content_type,
        body=signed_body if signed_body is not None else body,
    )
    headers = {
        'X-API-Key': api_key,
        'X-Signature-Version': '2',
        'X-Timestamp': timestamp_text,
        'X-Nonce': nonce,
        'X-Signature': sign_request_v2(secret, canonical),
        'Content-Type': CONTENT_TYPE,
    }
    if idempotency_key is not None:
        headers['Idempotency-Key'] = idempotency_key
    return headers


def _payload(
    external_id: str,
    *,
    amount: str = '100.00',
    metadata: dict | None = None,
) -> bytes:
    return json.dumps(
        {
            'external_id': external_id,
            'amount': amount,
            'currency': 'RUB',
            'method': 'sbp',
            'destination': '+79990001122',
            'metadata': metadata or {},
        },
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')


def _error_code(response: httpx.Response) -> str:
    return response.json()['error']['code']


async def _setup_merchant() -> tuple[str, str, uuid.UUID]:
    engine = create_async_engine(_database_url())
    suffix = uuid.uuid4().hex
    secret = f'hmac-v2-test-secret-{suffix}'
    api_key_value = f'hmac_v2_{suffix}'
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text('TRUNCATE TABLE users RESTART IDENTITY CASCADE')
            )
        async with AsyncSession(engine, expire_on_commit=False) as db:
            owner = User(
                email=f'hmac-v2-{suffix}@example.test',
                password_hash=hash_password(f'Password-{suffix}'),
                role=Role.merchant.value,
            )
            db.add(owner)
            await db.flush()
            merchant = Merchant(
                owner_id=owner.id,
                name=f'HMAC v2 merchant {suffix}',
                sandbox_mode=True,
            )
            db.add(merchant)
            await db.flush()
            db.add_all(
                [
                    ApiKey(
                        merchant_id=merchant.id,
                        api_key=api_key_value,
                        secret_hash=encrypt_secret(secret),
                        mode='sandbox',
                        is_active=True,
                    ),
                    Balance(
                        merchant_id=merchant.id,
                        currency='RUB',
                        available=Decimal('1000.00'),
                        frozen=Decimal('0.00'),
                    ),
                ]
            )
            await db.commit()
            merchant_id = merchant.id
    finally:
        await engine.dispose()

    redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        await redis.flushdb()
    finally:
        await redis.aclose()
    return api_key_value, secret, merchant_id


def test_hmac_v2_normative_vector_and_tamper_detection():
    fixture_path = (
        Path(__file__).parent / 'fixtures' / 'hmac_v2_vectors.json'
    )
    vector = json.loads(fixture_path.read_text(encoding='utf-8'))['vectors'][0]
    canonical = canonical_request_v2(
        timestamp=vector['timestamp'],
        nonce=vector['nonce'],
        method=vector['method'],
        path=vector['path'],
        query=vector['query'],
        content_type=vector['content_type'],
        body=vector['body_utf8'].encode('utf-8'),
    )
    assert canonical == vector['canonical']
    assert sign_request_v2(vector['secret'], canonical) == vector['signature']
    assert verify_request_v2(
        vector['secret'],
        canonical,
        vector['signature'],
    )

    variants = [
        canonical_request_v2(
            timestamp=vector['timestamp'],
            nonce=vector['nonce'],
            method='GET',
            path=vector['path'],
            query=vector['query'],
            content_type=vector['content_type'],
            body=vector['body_utf8'].encode(),
        ),
        canonical.replace('/merchant/deposits', '/merchant/payouts'),
        canonical.replace('a=first', 'a=changed'),
        canonical.replace('application/json', 'text/plain'),
        canonical[:-1] + ('0' if canonical[-1] != '0' else '1'),
    ]
    assert all(item != canonical for item in variants)
    assert all(
        not verify_request_v2(vector['secret'], item, vector['signature'])
        for item in variants
    )


def test_hmac_v2_replay_idempotency_tamper_and_fail_closed():
    async def scenario():
        await application_engine.dispose(close=False)
        api_key, secret, merchant_id = await _setup_merchant()
        transport = httpx.ASGITransport(
            app=app,
            raise_app_exceptions=False,
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url='https://localhost',
        ) as client:
            first_body = _payload(
                'payout-hmac-1',
                metadata={'customer_id': 'customer-1'},
            )
            first_headers = _signed_headers(
                api_key=api_key,
                secret=secret,
                body=first_body,
                nonce='nonce-first-00000001',
                idempotency_key='payout-hmac-idem-1',
            )
            first = await client.post(
                API_PATH,
                content=first_body,
                headers=first_headers,
            )
            assert first.status_code == 200, first.text
            payout_id = first.json()['id']

            exact_replay = await client.post(
                API_PATH,
                content=first_body,
                headers=first_headers,
            )
            assert exact_replay.status_code == 409
            assert _error_code(exact_replay) == 'hmac_replay_detected'

            retry = await client.post(
                API_PATH,
                content=first_body,
                headers=_signed_headers(
                    api_key=api_key,
                    secret=secret,
                    body=first_body,
                    nonce='nonce-retry-00000001',
                    idempotency_key='payout-hmac-idem-1',
                ),
            )
            assert retry.status_code == 200, retry.text
            assert retry.json()['id'] == payout_id
            assert retry.json()['idempotent'] is True

            changed_metadata = _payload(
                'payout-hmac-1',
                metadata={'customer_id': 'customer-2'},
            )
            conflict = await client.post(
                API_PATH,
                content=changed_metadata,
                headers=_signed_headers(
                    api_key=api_key,
                    secret=secret,
                    body=changed_metadata,
                    nonce='nonce-conflict-000001',
                    idempotency_key='payout-hmac-idem-1',
                ),
            )
            assert conflict.status_code == 409
            assert _error_code(conflict) == 'idempotency_conflict'

            tamper_cases = [
                {
                    'nonce': 'nonce-method-00000001',
                    'method': 'GET',
                },
                {
                    'nonce': 'nonce-path-000000001',
                    'signed_path': '/api/v1/merchant/wrong',
                },
                {
                    'nonce': 'nonce-query-00000001',
                    'signed_query': 'a=1',
                    'url': f'{API_PATH}?a=2',
                },
                {
                    'nonce': 'nonce-content-0000001',
                    'signed_content_type': 'application/json; charset=utf-8',
                },
                {
                    'nonce': 'nonce-body-000000001',
                    'signed_body': _payload('different-signed-body'),
                },
            ]
            for index, case in enumerate(tamper_cases):
                request_url = case.pop('url', API_PATH)
                nonce = case.pop('nonce')
                response = await client.post(
                    request_url,
                    content=_payload(f'tamper-{index}'),
                    headers=_signed_headers(
                        api_key=api_key,
                        secret=secret,
                        body=_payload(f'tamper-{index}'),
                        nonce=nonce,
                        idempotency_key=f'tamper-idem-{index}',
                        **case,
                    ),
                )
                assert response.status_code == 401, response.text
                assert _error_code(response) == 'hmac_invalid_signature'

            for label, timestamp in (
                (
                    'expired',
                    int(time.time())
                    - settings.HMAC_TIMESTAMP_TOLERANCE_SECONDS
                    - 5,
                ),
                (
                    'future',
                    int(time.time())
                    + settings.HMAC_TIMESTAMP_TOLERANCE_SECONDS
                    + 5,
                ),
            ):
                body = _payload(f'{label}-timestamp')
                response = await client.post(
                    API_PATH,
                    content=body,
                    headers=_signed_headers(
                        api_key=api_key,
                        secret=secret,
                        body=body,
                        nonce=f'nonce-{label}-0000001',
                        idempotency_key=f'{label}-idem',
                        timestamp=timestamp,
                    ),
                )
                assert response.status_code == 401
                assert _error_code(response) == 'hmac_expired'

            missing_nonce_body = _payload('missing-nonce')
            missing_nonce_headers = _signed_headers(
                api_key=api_key,
                secret=secret,
                body=missing_nonce_body,
                nonce='nonce-temporary-000001',
                idempotency_key='missing-nonce-idem',
            )
            del missing_nonce_headers['X-Nonce']
            missing_nonce = await client.post(
                API_PATH,
                content=missing_nonce_body,
                headers=missing_nonce_headers,
            )
            assert missing_nonce.status_code == 401
            assert _error_code(missing_nonce) == 'hmac_missing_header'
            assert missing_nonce.json()['error']['request_id']

            long_key_body = _payload('long-key')
            long_key = await client.post(
                API_PATH,
                content=long_key_body,
                headers=_signed_headers(
                    api_key=api_key,
                    secret=secret,
                    body=long_key_body,
                    nonce='nonce-long-key-000001',
                    idempotency_key='x' * 129,
                ),
            )
            assert long_key.status_code == 400
            assert _error_code(long_key) == 'idempotency_key_invalid'

            class UnavailableRedis:
                async def set(self, *args, **kwargs):
                    raise ConnectionError('test redis unavailable')

            unavailable_body = _payload('redis-unavailable')
            original_replay_redis = auth_deps._merchant_replay_redis
            auth_deps._merchant_replay_redis = UnavailableRedis()
            try:
                unavailable = await client.post(
                    API_PATH,
                    content=unavailable_body,
                    headers=_signed_headers(
                        api_key=api_key,
                        secret=secret,
                        body=unavailable_body,
                        nonce='nonce-redis-down-0001',
                        idempotency_key='redis-down-idem',
                    ),
                )
            finally:
                auth_deps._merchant_replay_redis = original_replay_redis
            assert unavailable.status_code == 503
            assert _error_code(unavailable) == (
                'replay_protection_unavailable'
            )

            legacy_body = _payload('legacy-v1', amount='50.00')
            legacy_timestamp = str(int(time.time()))
            legacy_headers = {
                'X-API-Key': api_key,
                'X-Timestamp': legacy_timestamp,
                'X-Signature': sign_hmac(
                    secret,
                    legacy_timestamp,
                    legacy_body,
                ),
                'Content-Type': CONTENT_TYPE,
            }
            disabled_v1 = await client.post(
                API_PATH,
                content=legacy_body,
                headers=legacy_headers,
            )
            assert disabled_v1.status_code == 401
            assert _error_code(disabled_v1) == 'hmac_missing_header'

            settings.MERCHANT_HMAC_V1_ENABLED = True
            try:
                enabled_v1 = await client.post(
                    API_PATH,
                    content=legacy_body,
                    headers=legacy_headers,
                )
            finally:
                settings.MERCHANT_HMAC_V1_ENABLED = False
            assert enabled_v1.status_code == 200, enabled_v1.text

            race_body = _payload('payout-hmac-race', amount='75.00')
            race_headers = [
                _signed_headers(
                    api_key=api_key,
                    secret=secret,
                    body=race_body,
                    nonce=f'nonce-idem-race-{index:04d}',
                    idempotency_key='payout-hmac-race-idem',
                )
                for index in range(2)
            ]
            race_results = await asyncio.gather(
                *[
                    client.post(
                        API_PATH,
                        content=race_body,
                        headers=headers,
                    )
                    for headers in race_headers
                ]
            )
            assert [response.status_code for response in race_results] == [
                200,
                200,
            ]
            race_ids = {response.json()['id'] for response in race_results}
            assert len(race_ids) == 1
            assert sum(
                bool(response.json().get('idempotent'))
                for response in race_results
            ) == 1

            replay_race_body = _payload('nonce-race')
            replay_race_headers = _signed_headers(
                api_key=api_key,
                secret=secret,
                body=replay_race_body,
                nonce='nonce-atomic-race-0001',
                idempotency_key='nonce-race-idem',
            )
            replay_race = await asyncio.gather(
                *[
                    client.post(
                        API_PATH,
                        content=replay_race_body,
                        headers=replay_race_headers,
                    )
                    for _ in range(2)
                ]
            )
            assert sorted(
                response.status_code for response in replay_race
            ) == [200, 409]
            rejected = next(
                response
                for response in replay_race
                if response.status_code == 409
            )
            assert _error_code(rejected) == 'hmac_replay_detected'

        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine) as db:
                assert await db.scalar(
                    select(func.count(Payout.id)).where(
                        Payout.merchant_id == merchant_id
                    )
                ) == 4
                balance = await db.scalar(
                    select(Balance).where(
                        Balance.merchant_id == merchant_id,
                        Balance.currency == 'RUB',
                    )
                )
                assert balance.available == Decimal('675.00')
                assert balance.frozen == Decimal('325.00')
                assert await db.scalar(
                    select(func.count(LedgerEntry.id)).where(
                        LedgerEntry.merchant_id == merchant_id,
                        LedgerEntry.entry_type == 'hold',
                    )
                ) == 4
        finally:
            await engine.dispose()
            await application_engine.dispose()

    asyncio.run(scenario())
