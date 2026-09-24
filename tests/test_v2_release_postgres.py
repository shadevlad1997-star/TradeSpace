import asyncio
import hashlib
import io
import logging
import os
import re
import socket
import uuid
from datetime import timedelta

import httpx
import pyotp
import pytest
import qrcode
from PIL import Image
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.enums import Role
from app.core.security import decrypt_secret, encrypt_secret, hash_password
from app.db.session import engine as application_engine
from app.main import app
from app.models import (
    AIIntegrationConfig,
    AuditLog,
    Deposit,
    PlatformCryptoWallet,
    User,
)
from app.services.ai_office import (
    AIConnectionResult,
    AIConnectionSnapshot,
    AIIntegrationError,
    ai_config_view,
    clear_ai_integration_secret,
    get_ai_integration_config,
    perform_ai_connection_test,
    run_ai_connection_test_without_transaction,
    save_ai_integration_config,
)
from app.services.platform_wallet import (
    PlatformWalletError,
    get_active_platform_wallet,
    list_platform_wallet_history,
    platform_wallet_qr_payload,
    set_active_platform_wallet,
)
from scripts import preflight_v2
from scripts.smoke_v2 import run_smoke


BASE58_ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'


def _database_url() -> str:
    value = os.getenv('TEST_DATABASE_URL', '')
    if not value:
        pytest.skip('TEST_DATABASE_URL is required for v2 PostgreSQL tests')
    if value.startswith('postgresql://'):
        return value.replace('postgresql://', 'postgresql+asyncpg://', 1)
    return value


def _base58_encode(value: bytes) -> str:
    number = int.from_bytes(value, byteorder='big')
    encoded = ''
    while number:
        number, remainder = divmod(number, 58)
        encoded = BASE58_ALPHABET[remainder] + encoded
    padding = len(value) - len(value.lstrip(b'\x00'))
    return '1' * padding + (encoded or '1')


def _base58check_address(
    *,
    version: int = 0x41,
    seed: bytes = bytes.fromhex('0011223344556677889900112233445566778899'),
) -> str:
    payload = bytes([version]) + seed[:20]
    checksum = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    return _base58_encode(payload + checksum)


def _decode_known_qr_png(png: bytes, candidates: tuple[str, ...]) -> str:
    actual = Image.open(io.BytesIO(png)).convert('1')
    for candidate in candidates:
        expected = qrcode.make(
            candidate,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            border=4,
        ).convert('1')
        if actual.size == expected.size and actual.tobytes() == expected.tobytes():
            return candidate
    raise AssertionError('QR image does not encode any expected wallet address')


WALLET_A = _base58check_address()
WALLET_B = _base58check_address(
    seed=bytes.fromhex('1122334455667788990011223344556677889900')
)
WALLET_C = _base58check_address(
    seed=bytes.fromhex('ffeeddccbbaa99887766554433221100ffeeddcc')
)
WRONG_NETWORK_WALLET = _base58check_address(version=0x42)
BAD_CHECKSUM_WALLET = WALLET_A[:-1] + ('1' if WALLET_A[-1] != '1' else '2')
OTP_SECRET = 'JBSWY3DPEHPK3PXP'


async def _reset_v2_tables() -> None:
    engine = create_async_engine(_database_url())
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    'TRUNCATE TABLE ai_integration_configs, '
                    'platform_crypto_wallets RESTART IDENTITY CASCADE'
                )
            )
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def clean_v2_tables():
    asyncio.run(_reset_v2_tables())
    yield
    asyncio.run(_reset_v2_tables())


def _csrf_from_html(html) -> str:
    text_value = html.text if hasattr(html, 'text') else str(html)
    match = re.search(r'name="csrf_token" value="([^"]+)"', text_value)
    assert match, text_value[:500]
    return match.group(1)


async def _actors(db: AsyncSession) -> dict[str, User]:
    suffix = uuid.uuid4().hex
    roles = {
        'superadmin': Role.superadmin.value,
        'admin': Role.admin.value,
        'support': Role.support.value,
        'teamlead': Role.teamlead.value,
        'operator': Role.operator.value,
        'trader': Role.trader.value,
        'merchant': Role.merchant.value,
    }
    actors = {}
    for name, role in roles.items():
        actors[name] = User(
            email=f'v2-{name}-{suffix}@example.test',
            password_hash=hash_password(f'{name}-v2-test-password'),
            role=role,
            twofa_enabled=role in {
                Role.superadmin.value,
                Role.admin.value,
                Role.support.value,
                Role.teamlead.value,
            },
            twofa_secret=(
                encrypt_secret(OTP_SECRET)
                if role
                in {
                    Role.superadmin.value,
                    Role.admin.value,
                    Role.support.value,
                    Role.teamlead.value,
                }
                else None
            ),
        )
        db.add(actors[name])
    await db.flush()
    return actors


async def _login(
    client: httpx.AsyncClient,
    actor: User,
    *,
    name: str,
) -> httpx.Response:
    realm = (
        'staff'
        if actor.role
        in {
            Role.superadmin.value,
            Role.admin.value,
            Role.support.value,
            Role.teamlead.value,
        }
        else actor.role
    )
    if realm == Role.operator.value:
        realm = 'trader'
    page = await client.get(f'/{realm}/login')
    data = {
        'csrf_token': _csrf_from_html(page),
        'email': actor.email,
        'password': f'{name}-v2-test-password',
    }
    if actor.twofa_enabled:
        data['otp'] = pyotp.TOTP(OTP_SECRET).now()
    return await client.post(f'/{realm}/login', data=data)


def test_platform_wallet_history_checksum_and_database_uniqueness():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                actors = await _actors(db)
                superadmin_id = actors['superadmin'].id
                first = await set_active_platform_wallet(
                    db,
                    address=WALLET_A,
                    label='Primary',
                    actor_id=superadmin_id,
                    change_reason='initial QA wallet',
                )
                await db.commit()
                assert first.changed is True
                assert first.wallet.version == 1

                repeated = await set_active_platform_wallet(
                    db,
                    address=WALLET_A,
                    label='Primary',
                    actor_id=superadmin_id,
                    change_reason='browser retry',
                )
                await db.commit()
                assert repeated.changed is False
                assert repeated.wallet.id == first.wallet.id
                assert await db.scalar(
                    select(func.count(PlatformCryptoWallet.id))
                ) == 1

                second = await set_active_platform_wallet(
                    db,
                    address=WALLET_B,
                    label='Replacement',
                    actor_id=superadmin_id,
                    change_reason='scheduled rotation',
                )
                await db.commit()
                assert second.changed is True
                assert second.previous_wallet_id == first.wallet.id
                assert second.wallet.version == 2
                history = await list_platform_wallet_history(db)
                assert [item.version for item in history] == [2, 1]
                assert history[0].is_active is True
                assert history[1].is_active is False
                assert history[1].address == WALLET_A
                assert history[1].deactivated_at is not None
                assert await db.scalar(
                    select(func.count(PlatformCryptoWallet.id)).where(
                        PlatformCryptoWallet.is_active.is_(True)
                    )
                ) == 1

                duplicate = PlatformCryptoWallet(
                    asset='USDT',
                    network='TRC20',
                    address=WALLET_A,
                    is_active=True,
                    version=3,
                    created_by=superadmin_id,
                    change_reason='must fail partial unique index',
                )
                db.add(duplicate)
                with pytest.raises(IntegrityError):
                    await db.flush()
                await db.rollback()

            async with AsyncSession(engine) as db:
                for invalid, code in (
                    (BAD_CHECKSUM_WALLET, 'invalid_trc20_address'),
                    (WRONG_NETWORK_WALLET, 'invalid_trc20_network'),
                ):
                    with pytest.raises(PlatformWalletError, match=code):
                        await set_active_platform_wallet(
                            db,
                            address=invalid,
                            label=None,
                            actor_id=superadmin_id,
                            change_reason='invalid test',
                        )
                    await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_concurrent_platform_wallet_replacements_keep_one_active_version():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as setup:
                actors = await _actors(setup)
                actor_id = actors['superadmin'].id
                await set_active_platform_wallet(
                    setup,
                    address=WALLET_A,
                    label='Initial',
                    actor_id=actor_id,
                    change_reason='concurrency baseline',
                )
                await setup.commit()

            async def replace(address: str, label: str):
                async with AsyncSession(engine, expire_on_commit=False) as db:
                    change = await set_active_platform_wallet(
                        db,
                        address=address,
                        label=label,
                        actor_id=actor_id,
                        change_reason=f'concurrent replacement {label}',
                    )
                    await db.commit()
                    return change.wallet.version

            versions = await asyncio.gather(
                replace(WALLET_B, 'B'),
                replace(WALLET_C, 'C'),
            )
            assert sorted(versions) == [2, 3]

            async with AsyncSession(engine) as verify:
                history = await list_platform_wallet_history(verify)
                assert [wallet.version for wallet in history] == [3, 2, 1]
                assert sum(wallet.is_active for wallet in history) == 1
                assert history[0].address in {WALLET_B, WALLET_C}
                assert {wallet.address for wallet in history} == {
                    WALLET_A,
                    WALLET_B,
                    WALLET_C,
                }
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_wallet_routes_permissions_empty_state_and_qr_from_active_database_row():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            await application_engine.dispose(close=False)
            async with AsyncSession(engine, expire_on_commit=False) as setup:
                actors = await _actors(setup)
                no_2fa = User(
                    email=f'v2-wallet-no-2fa-{uuid.uuid4().hex}@example.test',
                    password_hash=hash_password('no2fa-v2-test-password'),
                    role=Role.superadmin.value,
                    twofa_enabled=False,
                )
                setup.add(no_2fa)
                await setup.commit()

            transport = httpx.ASGITransport(
                app=app,
                raise_app_exceptions=False,
            )
            async with httpx.AsyncClient(
                transport=transport,
                base_url='https://localhost',
                follow_redirects=False,
            ) as trader_client:
                login = await _login(
                    trader_client,
                    actors['trader'],
                    name='trader',
                )
                assert login.status_code == 303
                empty = await trader_client.get('/trader/cabinet/tradespace/finance')
                assert empty.status_code == 200
                assert 'Платформенный кошелёк пока не указан' in empty.text
                assert 'alt="QR-код активного адреса пополнения"' not in empty.text
                missing_qr = await trader_client.get(
                    '/trader/cabinet/platform-wallet/qr'
                )
                assert missing_qr.status_code == 404

            async with httpx.AsyncClient(
                transport=transport,
                base_url='https://localhost',
                follow_redirects=False,
            ) as super_client:
                login = await _login(
                    super_client,
                    actors['superadmin'],
                    name='superadmin',
                )
                assert login.status_code == 303
                cabinet = await super_client.get('/staff/cabinet/tradespace/finance?tab=wallet')
                assert 'data-component="PlatformWallet"' in cabinet.text
                assert 'action="/staff/cabinet/platform-wallet"' in cabinet.text
                assert 'Платформенный USDT TRC20' in cabinet.text
                missing_csrf = await super_client.post(
                    '/staff/cabinet/platform-wallet',
                    data={
                        'address': WALLET_A,
                        'label': 'QA wallet',
                        'change_reason': 'must fail CSRF',
                    },
                )
                assert missing_csrf.status_code == 403
                missing_reason = await super_client.post(
                    '/staff/cabinet/platform-wallet',
                    data={
                        'csrf_token': _csrf_from_html(cabinet),
                        'address': WALLET_A,
                        'label': 'QA wallet',
                        'change_reason': ' ',
                    },
                )
                assert missing_reason.status_code == 303
                assert (
                    'platform_wallet_change_reason_required'
                    in missing_reason.headers['location']
                )
                saved = await super_client.post(
                    '/staff/cabinet/platform-wallet',
                    data={
                        'csrf_token': _csrf_from_html(cabinet),
                        'address': WALLET_A,
                        'label': 'QA wallet',
                        'change_reason': 'route QA',
                    },
                )
                assert saved.status_code == 303
                assert 'wallet' in saved.headers['location']
                configured = await super_client.get('/staff/cabinet/tradespace/finance?tab=wallet')
                assert WALLET_A in configured.text
                assert actors['superadmin'].email in configured.text
                assert (
                    'QR-код создаётся автоматически из активного адреса'
                    in configured.text
                )

            async with AsyncSession(engine) as verify:
                wallet = await get_active_platform_wallet(verify)
                assert wallet is not None
                assert wallet.address == WALLET_A
                assert platform_wallet_qr_payload(wallet) == WALLET_A

            for name in ('admin', 'operator', 'trader'):
                actor = actors[name]
                realm = 'staff' if name == 'admin' else 'trader'
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url='https://localhost',
                    follow_redirects=False,
                ) as client:
                    login = await _login(client, actor, name=name)
                    assert login.status_code == 303
                    cabinet = await client.get(f'/{realm}/cabinet/tradespace/finance' + ('?tab=wallet' if name == 'admin' else ''))
                    assert cabinet.status_code == 200
                    assert WALLET_A in cabinet.text
                    assert 'USDT' in cabinet.text
                    assert 'TRC20' in cabinet.text
                    assert 'Переводы в другой сети могут быть потеряны' in cabinet.text
                    if name == 'admin':
                        assert 'Платформенный USDT TRC20' in cabinet.text
                        assert 'История версий' in cabinet.text
                        assert actors['superadmin'].email in cabinet.text
                        assert (
                            'action="/staff/cabinet/platform-wallet"'
                            not in cabinet.text
                        )
                    else:
                        assert 'Платформенный USDT TRC20' not in cabinet.text
                        assert 'История версий' not in cabinet.text
                    qr = await client.get(
                        f'/{realm}/cabinet/platform-wallet/qr?v=1'
                    )
                    assert qr.status_code == 200
                    assert qr.headers['content-type'] == 'image/png'
                    assert qr.headers['x-platform-wallet-version'] == '1'
                    assert qr.headers['cache-control'].startswith('private')
                    assert qr.content.startswith(b'\x89PNG')
                    assert _decode_known_qr_png(
                        qr.content,
                        (WALLET_A, WALLET_B),
                    ) == WALLET_A
                    not_modified = await client.get(
                        f'/{realm}/cabinet/platform-wallet/qr?v=1',
                        headers={'If-None-Match': qr.headers['etag']},
                    )
                    assert not_modified.status_code == 304
                    denied = await client.post(
                        f'/{realm}/cabinet/platform-wallet',
                        data={
                            'csrf_token': _csrf_from_html(cabinet),
                            'address': WALLET_B,
                            'label': 'denied',
                            'change_reason': 'denied',
                        },
                    )
                    assert denied.status_code == 403

            for name in ('support', 'merchant', 'teamlead'):
                actor = actors[name]
                realm = (
                    'staff'
                    if name in {'support', 'teamlead'}
                    else 'merchant'
                )
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url='https://localhost',
                    follow_redirects=False,
                ) as client:
                    login = await _login(client, actor, name=name)
                    assert login.status_code == 303
                    cabinet = await client.get(f'/{realm}/cabinet')
                    assert cabinet.status_code == 200
                    assert 'Платформенный USDT TRC20' not in cabinet.text
                    qr = await client.get(
                        f'/{realm}/cabinet/platform-wallet/qr'
                    )
                    assert qr.status_code == 403

            async with httpx.AsyncClient(
                transport=transport,
                base_url='https://localhost',
                follow_redirects=False,
            ) as client:
                login = await _login(client, no_2fa, name='no2fa')
                assert login.status_code == 303
                reader_denied = await client.get('/staff/cabinet/tradespace/finance?tab=wallet')
                assert reader_denied.status_code == 200
                assert 'Для этой роли обязательна 2FA' in reader_denied.text
                assert 'data-component="PlatformWallet"' not in reader_denied.text
                assert WALLET_A not in reader_denied.text
                assert (await client.get('/staff/cabinet/platform-wallet/qr')).status_code == 403
                cabinet = await client.get('/staff/cabinet')
                denied = await client.post(
                    '/staff/cabinet/platform-wallet',
                    data={
                        'csrf_token': _csrf_from_html(cabinet),
                        'address': WALLET_B,
                        'label': 'must not be saved',
                        'change_reason': 'fresh 2FA required',
                    },
                )
                assert denied.status_code == 303
                assert 'ui_error=twofa_required' in denied.headers['location']

            async with httpx.AsyncClient(
                transport=transport,
                base_url='https://localhost',
                follow_redirects=False,
            ) as super_client:
                login = await _login(
                    super_client,
                    actors['superadmin'],
                    name='superadmin',
                )
                assert login.status_code == 303
                cabinet = await super_client.get('/staff/cabinet/tradespace/finance?tab=wallet')
                replaced = await super_client.post(
                    '/staff/cabinet/platform-wallet',
                    data={
                        'csrf_token': _csrf_from_html(cabinet),
                        'address': WALLET_B,
                        'label': 'QA replacement',
                        'change_reason': 'route QR replacement QA',
                    },
                )
                assert replaced.status_code == 303
                current_qr = await super_client.get(
                    '/staff/cabinet/platform-wallet/qr?v=2'
                )
                assert current_qr.status_code == 200
                assert current_qr.headers['etag'] == '"platform-wallet-v2"'
                assert current_qr.headers['x-platform-wallet-version'] == '2'
                assert _decode_known_qr_png(
                    current_qr.content,
                    (WALLET_A, WALLET_B),
                ) == WALLET_B
                history_page = await super_client.get(
                    '/staff/cabinet/tradespace/finance?tab=wallet'
                )
                assert WALLET_A in history_page.text
                assert WALLET_B in history_page.text
                assert 'route QR replacement QA' in history_page.text
                assert 'inactive' in history_page.text

            async with AsyncSession(engine) as verify:
                active = await get_active_platform_wallet(verify)
                assert active.address == WALLET_B
                assert active.version == 2
                assert await verify.scalar(
                    select(func.count(PlatformCryptoWallet.id))
                ) == 2
        finally:
            await application_engine.dispose()
            await engine.dispose()

    asyncio.run(scenario())


def test_ai_config_defaults_encryption_blank_update_and_separate_clear():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                actors = await _actors(db)
                assert ai_config_view(None)['enabled'] is False
                secret = 'qa-bearer-token-never-returned'
                config = await save_ai_integration_config(
                    db,
                    actor_id=actors['superadmin'].id,
                    enabled=False,
                    environment='local',
                    base_url='http://127.0.0.1:8000',
                    health_path='/api/health',
                    api_version='v1',
                    auth_type='bearer',
                    api_key='',
                    bearer_token=secret,
                    hmac_secret='',
                    timeout_seconds='5',
                    connect_timeout_seconds='2',
                    max_retries=0,
                    verify_tls=False,
                    selected_events=['deposit.paid', 'not.allowed'],
                )
                await db.commit()
                assert config.enabled is False
                assert config.inbound_commands_enabled is False
                assert config.encrypted_bearer_token != secret
                assert config.encrypted_bearer_token.startswith('enc:v1:')
                assert decrypt_secret(config.encrypted_bearer_token) == secret
                view = ai_config_view(config)
                assert view['bearer_token_configured'] is True
                assert secret not in repr(view)
                assert view['selected_events'] == ['deposit.paid']

                retained_ciphertext = config.encrypted_bearer_token
                config = await save_ai_integration_config(
                    db,
                    actor_id=actors['superadmin'].id,
                    enabled=False,
                    environment='local',
                    base_url='http://127.0.0.1:8000',
                    health_path='/api/health',
                    api_version='v1',
                    auth_type='bearer',
                    api_key='',
                    bearer_token='',
                    hmac_secret='',
                    timeout_seconds='5',
                    connect_timeout_seconds='2',
                    max_retries=0,
                    verify_tls=False,
                    selected_events=[],
                )
                await db.commit()
                assert config.encrypted_bearer_token == retained_ciphertext

                await clear_ai_integration_secret(
                    db,
                    actor_id=actors['superadmin'].id,
                    secret_kind='bearer_token',
                )
                await db.commit()
                assert config.encrypted_bearer_token is None

                with pytest.raises(
                    AIIntegrationError,
                    match='ai_office_production_activation_not_available',
                ):
                    await save_ai_integration_config(
                        db,
                        actor_id=actors['superadmin'].id,
                        enabled=True,
                        environment='production',
                        base_url='https://ai.example.test',
                        health_path='/api/health',
                        api_version=None,
                        auth_type='bearer',
                        api_key='',
                        bearer_token='production-is-blocked',
                        hmac_secret='',
                        timeout_seconds='5',
                        connect_timeout_seconds='2',
                        max_retries=0,
                        verify_tls=True,
                        selected_events=[],
                    )
                await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_ai_connection_ssrf_get_only_dns_recheck_timeout_and_redaction(caplog):
    async def scenario():
        requests: list[httpx.Request] = []
        resolution_calls = []

        def private_resolver(host, port, **kwargs):
            resolution_calls.append((host, port))
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    6,
                    '',
                    ('127.0.0.1', port),
                )
            ]

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                headers={'content-type': 'application/json'},
                json={'status': 'ok'},
            )

        staging = AIConnectionSnapshot(
            config_id=uuid.uuid4(),
            provider='veyra_ai_office',
            environment='staging',
            base_url='https://ai.example.test',
            health_path='/api/health',
            api_version=None,
            auth_type='bearer',
            api_key='',
            bearer_token='secret-that-must-not-be-logged',
            hmac_secret='',
            timeout_seconds=2,
            connect_timeout_seconds=1,
            max_retries=0,
            verify_tls=True,
        )
        blocked = await perform_ai_connection_test(
            staging,
            resolver=private_resolver,
            transport=httpx.MockTransport(handler),
        )
        assert blocked.success is False
        assert blocked.error_code == 'ai_office_private_address_blocked'
        assert requests == []

        rebound_calls = 0

        def rebound_resolver(host, port, **kwargs):
            nonlocal rebound_calls
            rebound_calls += 1
            address = (
                '93.184.216.34'
                if rebound_calls == 1
                else '127.0.0.1'
            )
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    6,
                    '',
                    (address, port),
                )
            ]

        rebound = await perform_ai_connection_test(
            staging,
            resolver=rebound_resolver,
            transport=httpx.MockTransport(handler),
        )
        assert rebound.success is False
        assert rebound.error_code == 'ai_office_private_address_blocked'
        assert rebound_calls == 2
        assert requests == []

        resolution_calls.clear()
        local = AIConnectionSnapshot(
            **{
                **staging.__dict__,
                'environment': 'local',
                'base_url': 'http://localhost:8000',
                'verify_tls': False,
            }
        )
        success = await perform_ai_connection_test(
            local,
            resolver=private_resolver,
            transport=httpx.MockTransport(handler),
        )
        assert success.success is True
        assert len(resolution_calls) == 2
        assert [request.method for request in requests] == ['GET']
        assert requests[0].headers['authorization'].startswith('Bearer ')

        async def timeout_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout('sensitive timeout details', request=request)

        caplog.set_level(logging.WARNING)
        timeout = await perform_ai_connection_test(
            local,
            resolver=private_resolver,
            transport=httpx.MockTransport(timeout_handler),
        )
        assert timeout.success is False
        assert timeout.error_code == 'ai_office_timeout'
        rendered_logs = '\n'.join(record.getMessage() for record in caplog.records)
        assert local.bearer_token not in rendered_logs
        assert local.bearer_token not in repr(timeout)

    asyncio.run(scenario())


def test_disabled_ai_config_has_no_background_worker_or_beat_task():
    from app.workers.celery_app import celery_app

    assert all('ai_office' not in name for name in celery_app.tasks)
    assert all(
        'ai_office' not in str(name)
        and 'ai_office' not in repr(schedule)
        for name, schedule in celery_app.conf.beat_schedule.items()
    )


def test_ai_connection_runner_closes_database_transaction_before_http():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine) as db:
                await db.execute(text('SELECT 1'))
                assert db.in_transaction()
                snapshot = AIConnectionSnapshot(
                    config_id=uuid.uuid4(),
                    provider='veyra_ai_office',
                    environment='local',
                    base_url='http://127.0.0.1:8000',
                    health_path='/api/health',
                    api_version=None,
                    auth_type='none',
                    api_key='',
                    bearer_token='',
                    hmac_secret='',
                    timeout_seconds=2,
                    connect_timeout_seconds=1,
                    max_retries=0,
                    verify_tls=False,
                )

                async def runner(value):
                    assert value == snapshot
                    assert not db.in_transaction()
                    return AIConnectionResult(
                        success=True,
                        status='success',
                        latency_ms=1,
                    )

                result = await run_ai_connection_test_without_transaction(
                    db,
                    snapshot,
                    runner=runner,
                )
                assert result.success is True
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_ai_office_routes_require_superadmin_2fa_csrf_and_never_render_secrets(
    monkeypatch,
):
    async def scenario():
        engine = create_async_engine(_database_url())
        secret = 'route-bearer-secret-must-stay-hidden'
        try:
            await application_engine.dispose(close=False)
            async with AsyncSession(engine, expire_on_commit=False) as setup:
                actors = await _actors(setup)
                no_2fa = User(
                    email=f'v2-no-2fa-{uuid.uuid4().hex}@example.test',
                    password_hash=hash_password('no2fa-v2-test-password'),
                    role=Role.superadmin.value,
                    twofa_enabled=False,
                )
                setup.add(no_2fa)
                await setup.commit()

            transport = httpx.ASGITransport(
                app=app,
                raise_app_exceptions=False,
            )
            async with httpx.AsyncClient(
                transport=transport,
                base_url='https://localhost',
                follow_redirects=False,
            ) as client:
                login = await _login(
                    client,
                    actors['superadmin'],
                    name='superadmin',
                )
                assert login.status_code == 303
                cabinet = await client.get('/staff/cabinet/tradespace/control?tab=ai')
                assert 'Настроить AI-интеграцию' in cabinet.text
                assert (
                    'Интеграция не должна включаться в production'
                    not in cabinet.text
                )
                assert '<h3>Интеграция с AI-офисом</h3>' not in cabinet.text
                assert 'action="/staff/cabinet/ai-office/config"' in cabinet.text
                assert 'name="auth_type"' in cabinet.text
                missing_csrf = await client.post(
                    '/staff/cabinet/ai-office/config',
                    data={'environment': 'local'},
                )
                assert missing_csrf.status_code == 403
                unsafe_enable = await client.post(
                    '/staff/cabinet/ai-office/config',
                    data={
                        'csrf_token': _csrf_from_html(cabinet),
                        'enabled': 'on',
                        'environment': 'staging',
                        'base_url': 'https://ai.example.test',
                        'health_path': '/api/health',
                        'auth_type': 'none',
                        'timeout_seconds': '5',
                        'connect_timeout_seconds': '2',
                        'max_retries': '0',
                        'change_reason': 'verify M2M activation guard',
                    },
                )
                assert unsafe_enable.status_code == 303
                assert (
                    'ai_office_m2m_auth_required'
                    in unsafe_enable.headers['location']
                )
                unsafe_page = await client.get(
                    '/staff/cabinet/tradespace/control'
                    '?tab=ai'
                    '&ui_error=ai_office_m2m_auth_required'
                )
                assert 'data-ui-code="ai_office_m2m_auth_required"' in unsafe_page.text
                assert (
                    'Выберите обязательный M2M Auth type'
                    in unsafe_page.text
                )
                saved = await client.post(
                    '/staff/cabinet/ai-office/config',
                    data={
                        'csrf_token': _csrf_from_html(cabinet),
                        'environment': 'local',
                        'base_url': 'http://127.0.0.1:8000',
                        'health_path': '/api/health',
                        'auth_type': 'bearer',
                        'bearer_token': secret,
                        'timeout_seconds': '5',
                        'connect_timeout_seconds': '2',
                        'max_retries': '0',
                        'change_reason': 'route config QA',
                    },
                )
                assert saved.status_code == 303
                reloaded = await client.get('/staff/cabinet/tradespace/control?tab=ai')
                assert secret not in reloaded.text
                assert 'configured' in reloaded.text

                async def fake_connection(snapshot):
                    assert snapshot.bearer_token == secret
                    return AIConnectionResult(
                        success=True,
                        status='success',
                        latency_ms=7,
                    )

                monkeypatch.setattr(
                    'app.services.ai_office.perform_ai_connection_test',
                    fake_connection,
                )
                tested = await client.post(
                    '/staff/cabinet/ai-office/test-connection',
                    data={'csrf_token': _csrf_from_html(reloaded)},
                )
                assert tested.status_code == 303

            async with AsyncSession(engine) as verify:
                config = await get_ai_integration_config(verify)
                assert decrypt_secret(config.encrypted_bearer_token) == secret
                assert config.last_connection_test_status == 'success'
                assert config.last_connection_test_latency_ms == 7
                audit_rows = (
                    await verify.execute(
                        select(AuditLog).where(
                            AuditLog.action.in_(
                                {
                                    'ai_office_config_updated',
                                    'ai_office_connection_tested',
                                }
                            )
                        )
                    )
                ).scalars().all()
                assert audit_rows
                assert secret not in repr([row.details for row in audit_rows])

            for name in ('admin', 'support', 'teamlead', 'trader', 'merchant'):
                actor = actors[name]
                realm = (
                    'staff'
                    if name in {'admin', 'support', 'teamlead'}
                    else name
                )
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url='https://localhost',
                    follow_redirects=False,
                ) as client:
                    login = await _login(client, actor, name=name)
                    assert login.status_code == 303
                    cabinet = await client.get(f'/{realm}/cabinet')
                    assert 'Интеграция с AI-офисом' not in cabinet.text
                    denied = await client.post(
                        f'/{realm}/cabinet/ai-office/config',
                        data={
                            'csrf_token': _csrf_from_html(cabinet),
                            'environment': 'local',
                            'base_url': 'http://127.0.0.1:8000',
                            'health_path': '/api/health',
                            'auth_type': 'none',
                            'timeout_seconds': '5',
                            'connect_timeout_seconds': '2',
                            'max_retries': '0',
                            'change_reason': 'must be denied',
                        },
                    )
                    assert denied.status_code == 303

            async with httpx.AsyncClient(
                transport=transport,
                base_url='https://localhost',
                follow_redirects=False,
            ) as client:
                page = await client.get('/staff/login')
                login = await client.post(
                    '/staff/login',
                    data={
                        'csrf_token': _csrf_from_html(page),
                        'email': no_2fa.email,
                        'password': 'no2fa-v2-test-password',
                    },
                )
                assert login.status_code == 303
                cabinet = await client.get('/staff/cabinet')
                denied = await client.post(
                    '/staff/cabinet/ai-office/config',
                    data={
                        'csrf_token': _csrf_from_html(cabinet),
                        'environment': 'local',
                        'base_url': 'http://127.0.0.1:8000',
                        'health_path': '/api/health',
                        'auth_type': 'none',
                        'timeout_seconds': '5',
                        'connect_timeout_seconds': '2',
                        'max_retries': '0',
                        'change_reason': 'must require 2FA',
                    },
                )
                assert denied.status_code == 303
                assert 'ui_error=twofa_required' in denied.headers['location']
        finally:
            await application_engine.dispose()
            await engine.dispose()

    asyncio.run(scenario())


def test_release_version_preflight_read_only_and_smoke_get_only(monkeypatch):
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            await application_engine.dispose(close=False)
            async with AsyncSession(engine) as db:
                before = {
                    'deposits': await db.scalar(select(func.count(Deposit.id))),
                    'wallets': await db.scalar(
                        select(func.count(PlatformCryptoWallet.id))
                    ),
                    'ai_configs': await db.scalar(
                        select(func.count(AIIntegrationConfig.id))
                    ),
                    'audits': await db.scalar(
                        select(func.count(AuditLog.id))
                    ),
                }
                revision_before = await db.scalar(
                    text('SELECT version_num FROM alembic_version')
                )

            async def fake_redis():
                return preflight_v2.Check(
                    'redis_connectivity',
                    True,
                    'ok',
                )

            async def fake_rapira():
                return preflight_v2.Check(
                    'rapira_live_ask',
                    True,
                    'ok symbol=USDT/RUB source=rapira_live freshness=fetched_at',
                )

            async def fake_health(base_url):
                return [
                    preflight_v2.Check('endpoint:/health', True, 'HTTP 200'),
                    preflight_v2.Check('endpoint:/ready', True, 'HTTP 200'),
                ]

            monkeypatch.setattr(preflight_v2, 'check_redis', fake_redis)
            monkeypatch.setattr(preflight_v2, 'check_rapira', fake_rapira)
            monkeypatch.setattr(preflight_v2, 'check_health', fake_health)
            checks = await preflight_v2.run_preflight(
                'http://127.0.0.1:8000'
            )
            rendered = '\n'.join(
                f'{item.name}: {item.detail}' for item in checks
            )
            from app.core.config import settings

            assert settings.DATABASE_URL not in rendered
            assert settings.SYNC_DATABASE_URL not in rendered
            assert settings.SECRET_KEY not in rendered
            assert settings.ENCRYPTION_KEY not in rendered

            async with AsyncSession(engine) as db:
                after = {
                    'deposits': await db.scalar(select(func.count(Deposit.id))),
                    'wallets': await db.scalar(
                        select(func.count(PlatformCryptoWallet.id))
                    ),
                    'ai_configs': await db.scalar(
                        select(func.count(AIIntegrationConfig.id))
                    ),
                    'audits': await db.scalar(
                        select(func.count(AuditLog.id))
                    ),
                }
                revision_after = await db.scalar(
                    text('SELECT version_num FROM alembic_version')
                )
            assert after == before
            assert revision_after == revision_before

            methods = []

            async def smoke_handler(request: httpx.Request):
                methods.append(request.method)
                if request.url.path == '/health':
                    return httpx.Response(
                        200,
                        json={
                            'status': 'ok',
                            'version': '2.0.0-rc2',
                        },
                    )
                if request.url.path == '/ready':
                    return httpx.Response(200, json={'status': 'ready'})
                if request.url.path == '/version':
                    return httpx.Response(
                        200,
                        json={'version': '2.0.0-rc2'},
                    )
                return httpx.Response(
                    200,
                    headers={'content-type': 'text/html; charset=utf-8'},
                    text='<form>staff login</form>',
                )

            smoke = await run_smoke(
                'https://localhost',
                transport=httpx.MockTransport(smoke_handler),
                openapi_enabled=False,
            )
            assert all(item.passed for item in smoke)
            assert methods == ['GET', 'GET', 'GET', 'GET']

            direct_transport = httpx.ASGITransport(
                app=app,
                raise_app_exceptions=False,
            )
            async with httpx.AsyncClient(
                transport=direct_transport,
                base_url='https://localhost',
            ) as client:
                response = await client.get('/version')
                assert response.status_code == 200
                assert response.json() == {'version': '2.0.0-rc2'}
        finally:
            await application_engine.dispose()
            await engine.dispose()

    asyncio.run(scenario())
