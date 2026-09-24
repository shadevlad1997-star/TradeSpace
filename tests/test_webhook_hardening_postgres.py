import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import app.services.webhook_enqueue as webhook_enqueue
from app.core.config import settings
from app.core.enums import Role
from app.core.security import (
    auth_state_marker,
    create_token,
    encrypt_secret,
    hash_password,
    verify_hmac,
)
from app.db.session import engine as application_engine
from app.main import app
from app.models import (
    ApiKey,
    AuditLog,
    Merchant,
    MerchantWebhookSigningKey,
    User,
    WebhookDeliveryAttempt,
    WebhookEvent,
)
from app.services.webhook_signing_keys import (
    delivery_webhook_signing_key,
    issue_webhook_signing_key,
    revoke_webhook_signing_key,
    rotate_webhook_signing_key,
    verification_webhook_signing_keys,
)
from app.services.webhooks import (
    WebhookHttpResult,
    deliver_event,
    due_webhook_event_ids,
    queue_webhook,
    request_manual_webhook_retry,
)


def _database_url() -> str:
    value = os.getenv('TEST_DATABASE_URL', '')
    if not value:
        pytest.skip('TEST_DATABASE_URL is required for webhook tests')
    if value.startswith('postgresql://'):
        return value.replace('postgresql://', 'postgresql+asyncpg://', 1)
    return value


async def _clean_database(engine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text('TRUNCATE TABLE users RESTART IDENTITY CASCADE')
        )


async def _merchant_fixture(
    db: AsyncSession,
    *,
    webhook_url: str = 'https://merchant-webhook.example.test/events',
) -> tuple[Merchant, User, str]:
    suffix = uuid.uuid4().hex
    api_secret = f'merchant-api-secret-{suffix}'
    owner = User(
        email=f'webhook-owner-{suffix}@example.test',
        password_hash=hash_password(f'Password-{suffix}'),
        role=Role.merchant.value,
    )
    db.add(owner)
    await db.flush()
    merchant = Merchant(
        owner_id=owner.id,
        name=f'Webhook merchant {suffix}',
        webhook_url=webhook_url,
    )
    db.add(merchant)
    await db.flush()
    db.add(
        ApiKey(
            merchant_id=merchant.id,
            api_key=f'pk_test_{suffix}',
            secret_hash=encrypt_secret(api_secret),
            mode='sandbox',
            is_active=True,
        )
    )
    await db.flush()
    return merchant, owner, api_secret


def test_webhook_signature_fixture():
    vector = json.loads(
        (
            Path(__file__).parent
            / 'fixtures'
            / 'webhook_signature_vectors.json'
        ).read_text(encoding='utf-8')
    )['vectors'][0]
    assert (
        f"{vector['timestamp']}.{vector['body_utf8']}"
        == vector['canonical']
    )
    from app.core.security import sign_hmac

    assert sign_hmac(
        vector['secret'],
        vector['timestamp'],
        vector['body_utf8'].encode('utf-8'),
    ) == vector['signature']


def test_dedicated_signing_key_rotation_overlap_and_revocation():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            await _clean_database(engine)
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, owner, api_secret = await _merchant_fixture(db)
                first, first_secret = await issue_webhook_signing_key(
                    db,
                    merchant,
                    created_by=owner.id,
                )
                await db.commit()
                assert first_secret not in first.encrypted_secret
                assert api_secret not in first.encrypted_secret
                assert first.key_id.startswith('whk_')

                second, second_secret, retiring = (
                    await rotate_webhook_signing_key(
                        db,
                        merchant,
                        created_by=owner.id,
                        overlap_seconds=120,
                    )
                )
                await db.commit()
                assert second_secret != first_secret
                assert retiring.id == first.id
                assert retiring.status == 'retiring'
                assert retiring.retire_at > datetime.now(timezone.utc)

                verification = await verification_webhook_signing_keys(
                    db,
                    merchant.id,
                )
                assert {key.id for key in verification} == {
                    first.id,
                    second.id,
                }
                preferred = await delivery_webhook_signing_key(
                    db,
                    merchant.id,
                    first.id,
                )
                assert preferred and preferred[0].id == first.id
                assert preferred[1] == first_secret

                await revoke_webhook_signing_key(db, first)
                await db.commit()
                replacement = await delivery_webhook_signing_key(
                    db,
                    merchant.id,
                    first.id,
                )
                assert replacement and replacement[0].id == second.id
                assert replacement[1] == second_secret
                verification = await verification_webhook_signing_keys(
                    db,
                    merchant.id,
                )
                assert [key.id for key in verification] == [second.id]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_delivery_success_uses_dedicated_secret_and_redacted_logs(caplog):
    async def scenario():
        engine = create_async_engine(_database_url())
        captured = {}
        try:
            await _clean_database(engine)
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, owner, api_secret = await _merchant_fixture(db)
                signing_key, webhook_secret = (
                    await issue_webhook_signing_key(
                        db,
                        merchant,
                        created_by=owner.id,
                    )
                )
                event = await queue_webhook(
                    db,
                    merchant.id,
                    'deposit.paid',
                    {'id': 'operation-1', 'amount': '1250.00'},
                )
                event_id = event.id
                await db.commit()

                async def success(url, body, headers, timeout_seconds):
                    captured.update(
                        url=url,
                        body=body,
                        headers=headers,
                        timeout_seconds=timeout_seconds,
                    )
                    return WebhookHttpResult(200)

                with caplog.at_level(logging.INFO, logger='app.webhooks'):
                    result = await deliver_event(
                        db,
                        event_id,
                        lock_owner='success-worker',
                        http_post=success,
                    )
                    await db.commit()
                assert result.status == 'delivered'
                assert result.attempts == 1
                assert result.signing_key_id == signing_key.id

                headers = captured['headers']
                assert headers['X-TradeSpace-Event-ID'] == str(event_id)
                assert headers['X-TradeSpace-Event-Type'] == 'deposit.paid'
                assert headers['X-TradeSpace-Key-ID'] == signing_key.key_id
                assert headers['X-TradeSpace-Signature-Version'] == '1'
                assert headers['X-Correlation-ID'] == event.correlation_id
                assert 'X-API-Key' not in headers
                assert all(
                    api_secret not in value
                    for value in headers.values()
                )
                assert verify_hmac(
                    webhook_secret,
                    headers['X-TradeSpace-Timestamp'],
                    captured['body'],
                    headers['X-TradeSpace-Signature'],
                )
                assert not verify_hmac(
                    api_secret,
                    headers['X-TradeSpace-Timestamp'],
                    captured['body'],
                    headers['X-TradeSpace-Signature'],
                )
                assert webhook_secret not in caplog.text
                assert api_secret not in caplog.text

                attempt = await db.scalar(
                    select(WebhookDeliveryAttempt).where(
                        WebhookDeliveryAttempt.webhook_event_id
                        == event_id
                    )
                )
                assert attempt.attempt_no == 1
                assert attempt.status == 'delivered'
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_retry_policy_terminal_dead_letter_and_configuration_required():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            await _clean_database(engine)
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, owner, _api_secret = await _merchant_fixture(db)
                await issue_webhook_signing_key(
                    db,
                    merchant,
                    created_by=owner.id,
                )

                final_event = await queue_webhook(
                    db,
                    merchant.id,
                    'deposit.failed',
                    {'id': 'last-attempt'},
                )
                final_event.max_attempts = 2
                await db.commit()

                async def server_error(*args, **kwargs):
                    return WebhookHttpResult(500)

                first = await deliver_event(
                    db,
                    final_event.id,
                    http_post=server_error,
                )
                await db.commit()
                assert first.status == 'retry_scheduled'
                assert first.attempts == 1
                first.next_attempt_at = (
                    datetime.now(timezone.utc) - timedelta(seconds=1)
                )
                await db.commit()
                second = await deliver_event(
                    db,
                    final_event.id,
                    http_post=server_error,
                )
                await db.commit()
                assert second.status == 'dead_letter'
                assert second.attempts == 2
                assert second.next_attempt_at is None

                timeout_event = await queue_webhook(
                    db,
                    merchant.id,
                    'payout.failed',
                    {'id': 'timeout'},
                )
                too_many_event = await queue_webhook(
                    db,
                    merchant.id,
                    'payout.failed',
                    {'id': 'too-many'},
                )
                bad_request_event = await queue_webhook(
                    db,
                    merchant.id,
                    'payout.failed',
                    {'id': 'bad-request'},
                )
                await db.commit()

                async def timeout(*args, **kwargs):
                    raise asyncio.TimeoutError()

                timeout_result = await deliver_event(
                    db,
                    timeout_event.id,
                    http_post=timeout,
                )
                await db.commit()
                assert timeout_result.status == 'retry_scheduled'
                assert timeout_result.last_error == 'network_timeout'

                before_429 = datetime.now(timezone.utc)

                async def too_many(*args, **kwargs):
                    return WebhookHttpResult(429, retry_after='120')

                too_many_result = await deliver_event(
                    db,
                    too_many_event.id,
                    http_post=too_many,
                )
                await db.commit()
                assert too_many_result.status == 'retry_scheduled'
                assert too_many_result.next_attempt_at >= (
                    before_429 + timedelta(seconds=119)
                )

                async def bad_request(*args, **kwargs):
                    return WebhookHttpResult(400)

                bad_result = await deliver_event(
                    db,
                    bad_request_event.id,
                    http_post=bad_request,
                )
                await db.commit()
                assert bad_result.status == 'dead_letter'
                assert bad_result.attempts == 1

                missing_merchant, _owner, _api_secret = (
                    await _merchant_fixture(db)
                )
                missing_event = await queue_webhook(
                    db,
                    missing_merchant.id,
                    'deposit.paid',
                    {'id': 'missing-key'},
                )
                await db.commit()
                missing = await deliver_event(db, missing_event.id)
                await db.commit()
                assert missing.status == 'configuration_required'
                assert missing.last_error == 'missing_webhook_signing_key'
                assert missing.attempts == 0

                invalid_merchant, invalid_owner, _api_secret = (
                    await _merchant_fixture(
                        db,
                        webhook_url='ftp://invalid.example.test/callback',
                    )
                )
                await issue_webhook_signing_key(
                    db,
                    invalid_merchant,
                    created_by=invalid_owner.id,
                )
                invalid_event = await queue_webhook(
                    db,
                    invalid_merchant.id,
                    'deposit.paid',
                    {'id': 'ssrf-guard'},
                )
                await db.commit()
                invalid = await deliver_event(db, invalid_event.id)
                await db.commit()
                assert invalid.status == 'dead_letter'
                assert invalid.attempts == 1
                assert 'webhook_url' in invalid.last_error

                due = await due_webhook_event_ids(db)
                assert final_event.id not in due
                assert bad_request_event.id not in due
                assert missing_event.id not in due
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_claim_race_stale_lease_scanner_enqueue_recovery_and_manual_retry(
    monkeypatch,
):
    async def scenario():
        engine = create_async_engine(_database_url())
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0
        try:
            await _clean_database(engine)
            async with AsyncSession(engine, expire_on_commit=False) as setup:
                merchant, owner, _api_secret = await _merchant_fixture(
                    setup
                )
                await issue_webhook_signing_key(
                    setup,
                    merchant,
                    created_by=owner.id,
                )
                race_event = await queue_webhook(
                    setup,
                    merchant.id,
                    'deposit.paid',
                    {'id': 'race'},
                )
                race_event_id = race_event.id
                await setup.commit()

            async def blocking_success(*args, **kwargs):
                nonlocal calls
                calls += 1
                started.set()
                await release.wait()
                return WebhookHttpResult(200)

            async def worker(owner_name):
                async with AsyncSession(
                    engine,
                    expire_on_commit=False,
                ) as db:
                    result = await deliver_event(
                        db,
                        race_event_id,
                        lock_owner=owner_name,
                        http_post=blocking_success,
                    )
                    await db.commit()
                    return result.status

            first_task = asyncio.create_task(worker('worker-a'))
            await asyncio.wait_for(started.wait(), timeout=5)
            second_status = await asyncio.wait_for(
                worker('worker-b'),
                timeout=5,
            )
            assert second_status == 'processing'
            release.set()
            assert await first_task == 'delivered'
            assert calls == 1

            async with AsyncSession(engine, expire_on_commit=False) as db:
                race_row = await db.get(WebhookEvent, race_event_id)
                assert race_row.status == 'delivered'
                assert race_row.attempts == 1
                assert await db.scalar(
                    select(
                        func.count(WebhookDeliveryAttempt.id)
                    ).where(
                        WebhookDeliveryAttempt.webhook_event_id
                        == race_event_id
                    )
                ) == 1

                stale = await queue_webhook(
                    db,
                    merchant.id,
                    'deposit.failed',
                    {'id': 'stale'},
                )
                stale.status = 'processing'
                stale.attempts = 1
                stale.max_attempts = 3
                stale.lock_owner = 'dead-worker'
                stale.locked_at = (
                    datetime.now(timezone.utc) - timedelta(minutes=5)
                )
                stale.lease_until = (
                    datetime.now(timezone.utc) - timedelta(seconds=1)
                )
                db.add(
                    WebhookDeliveryAttempt(
                        webhook_event_id=stale.id,
                        attempt_no=1,
                        status='processing',
                    )
                )
                pending = await queue_webhook(
                    db,
                    merchant.id,
                    'deposit.paid',
                    {'id': 'enqueue-failed'},
                )
                future_retry = await queue_webhook(
                    db,
                    merchant.id,
                    'deposit.failed',
                    {'id': 'future'},
                )
                future_retry.status = 'retry_scheduled'
                future_retry.next_attempt_at = (
                    datetime.now(timezone.utc) + timedelta(hours=1)
                )
                await db.commit()
                stale_id = stale.id
                pending_id = pending.id
                future_id = future_retry.id

            async def success(*args, **kwargs):
                return WebhookHttpResult(200)

            async with AsyncSession(engine, expire_on_commit=False) as db:
                stale_result = await deliver_event(
                    db,
                    stale_id,
                    lock_owner='recovery-worker',
                    http_post=success,
                )
                await db.commit()
                assert stale_result.status == 'delivered'
                assert stale_result.attempts == 2

                due = await due_webhook_event_ids(db)
                assert pending_id in due
                assert future_id not in due
                assert stale_id not in due

                def enqueue_failure(*args, **kwargs):
                    raise ConnectionError('broker unavailable')

                monkeypatch.setattr(
                    webhook_enqueue.deliver_webhook_task,
                    'delay',
                    enqueue_failure,
                )
                assert not webhook_enqueue.enqueue_webhook_delivery(
                    pending_id
                )
                db.expire_all()
                pending_row = await db.get(WebhookEvent, pending_id)
                assert pending_row.status == 'pending'
                assert pending_id in await due_webhook_event_ids(db)

                pending_row.status = 'dead_letter'
                pending_row.attempts = pending_row.max_attempts
                await db.commit()
                previous_attempts = pending_row.attempts
                retried = await request_manual_webhook_retry(
                    db,
                    pending_id,
                )
                await db.commit()
                assert retried.id == pending_id
                assert retried.status == 'pending'
                assert retried.attempts == previous_attempts
                assert retried.max_attempts > retried.attempts
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_manual_retry_route_is_superadmin_only_and_audited(monkeypatch):
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            await application_engine.dispose(close=False)
            await _clean_database(engine)
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, _owner, _api_secret = await _merchant_fixture(db)
                admin_hash = hash_password('Admin-test-password')
                super_hash = hash_password('Super-test-password')
                admin = User(
                    email=f'webhook-admin-{uuid.uuid4().hex}@example.test',
                    password_hash=admin_hash,
                    role=Role.admin.value,
                    twofa_enabled=True,
                )
                superadmin = User(
                    email=f'webhook-super-{uuid.uuid4().hex}@example.test',
                    password_hash=super_hash,
                    role=Role.superadmin.value,
                    twofa_enabled=True,
                )
                db.add_all([admin, superadmin])
                await db.flush()
                event = WebhookEvent(
                    merchant_id=merchant.id,
                    event_type='deposit.failed',
                    payload={'id': 'manual'},
                    status='dead_letter',
                    attempts=5,
                    max_attempts=5,
                    correlation_id=uuid.uuid4().hex,
                )
                db.add(event)
                await db.commit()
                event_id = event.id
                admin_token = create_token(
                    str(admin.id),
                    'access',
                    timedelta(minutes=5),
                    {'auth': auth_state_marker(admin_hash)},
                )
                super_token = create_token(
                    str(superadmin.id),
                    'access',
                    timedelta(minutes=5),
                    {'auth': auth_state_marker(super_hash)},
                )

            monkeypatch.setattr(
                'app.api.v1.admin._enqueue_webhook',
                lambda event_id: False,
            )
            transport = httpx.ASGITransport(
                app=app,
                raise_app_exceptions=False,
            )
            async with httpx.AsyncClient(
                transport=transport,
                base_url='https://localhost',
            ) as client:
                denied = await client.post(
                    f'/api/v1/admin/webhooks/{event_id}/retry',
                    headers={'Authorization': f'Bearer {admin_token}'},
                )
                assert denied.status_code == 403
                allowed = await client.post(
                    f'/api/v1/admin/webhooks/{event_id}/retry',
                    headers={'Authorization': f'Bearer {super_token}'},
                )
                assert allowed.status_code == 200, allowed.text
                assert allowed.json()['event_id'] == str(event_id)

            async with AsyncSession(engine) as db:
                event = await db.get(WebhookEvent, event_id)
                assert event.status == 'pending'
                assert event.attempts == 5
                assert event.max_attempts == 10
                audit_row = await db.scalar(
                    select(AuditLog).where(
                        AuditLog.action == 'webhook_retry_requested',
                        AuditLog.target_id == str(event_id),
                    )
                )
                assert audit_row is not None
        finally:
            await application_engine.dispose()
            await engine.dispose()

    asyncio.run(scenario())
