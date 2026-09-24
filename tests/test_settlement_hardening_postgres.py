import asyncio
import hashlib
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.enums import Role
from app.core.security import hash_password
from app.core.tron import TronAddressError, validate_trc20_address
from app.db.session import engine as application_engine
from app.main import app
from app.models import AuditLog, Balance, LedgerEntry, Merchant, MerchantSettlement, User
from app.services.rapira import RollingRapiraQuote, RollingRateUnavailable
from app.services.settlements import (
    MerchantSettlementConflict,
    MerchantSettlementRateUnavailable,
    complete_merchant_settlement,
    create_merchant_settlement,
    reject_merchant_settlement,
)


BASE58_ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
CONCURRENCY_TIMEOUT_SECONDS = 10


def _database_url() -> str:
    value = os.getenv('TEST_DATABASE_URL', '')
    if not value:
        pytest.skip('TEST_DATABASE_URL is required for settlement tests')
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


def _address(*, version: int = 0x41) -> str:
    body = bytes([version]) + bytes.fromhex(
        '0011223344556677889900112233445566778899'
    )
    checksum = hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4]
    return _base58_encode(body + checksum)


VALID_ADDRESS = _address()
WRONG_NETWORK_ADDRESS = _address(version=0x42)
BAD_CHECKSUM_ADDRESS = VALID_ADDRESS[:-1] + (
    '1' if VALID_ADDRESS[-1] != '1' else '2'
)


def _quote(
    *,
    fetched_at: datetime | None = None,
    provider_timestamp: datetime | None = None,
    rate: str = '100',
) -> RollingRapiraQuote:
    fetched = fetched_at or datetime.now(timezone.utc)
    return RollingRapiraQuote(
        symbol='USDT/RUB',
        rate=Decimal(rate),
        side='ask',
        source='rapira_live',
        provider_timestamp=provider_timestamp,
        fetched_at=fetched,
        freshness_basis=(
            'provider_timestamp'
            if provider_timestamp is not None
            else 'fetched_at'
        ),
        stale=False,
        provider_field='askPrice',
    )


async def _merchant_fixture(
    db: AsyncSession,
    *,
    available: str = '10000.00',
) -> tuple[Merchant, User, User, Balance]:
    suffix = uuid.uuid4().hex
    owner = User(
        email=f'settlement-merchant-{suffix}@example.test',
        password_hash=hash_password(f'Password-{suffix}'),
        role=Role.merchant.value,
    )
    actor = User(
        email=f'settlement-superadmin-{suffix}@example.test',
        password_hash=hash_password(f'Password-{suffix}'),
        role=Role.superadmin.value,
        twofa_enabled=True,
    )
    db.add_all([owner, actor])
    await db.flush()
    merchant = Merchant(
        owner_id=owner.id,
        name=f'Settlement merchant {suffix}',
    )
    db.add(merchant)
    await db.flush()
    balance = Balance(
        merchant_id=merchant.id,
        currency='RUB',
        available=Decimal(available),
        frozen=Decimal('0.00'),
    )
    db.add(balance)
    await db.flush()
    return merchant, owner, actor, balance


def test_trc20_validator_checks_checksum_and_network():
    assert validate_trc20_address(VALID_ADDRESS) == VALID_ADDRESS
    with pytest.raises(TronAddressError, match='invalid_trc20_address'):
        validate_trc20_address(BAD_CHECKSUM_ADDRESS)
    with pytest.raises(TronAddressError, match='invalid_trc20_network'):
        validate_trc20_address(WRONG_NETWORK_ADDRESS)


def test_unavailable_live_rate_does_not_mutate_finance(monkeypatch):
    import app.services.settlements as settlements

    async def unavailable():
        raise RollingRateUnavailable('rolling_rate_unavailable')

    monkeypatch.setattr(
        settlements,
        'get_strict_rolling_ask_quote',
        unavailable,
    )

    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, owner, _actor, balance = await _merchant_fixture(db)
                before = (balance.available, balance.frozen)
                with pytest.raises(
                    MerchantSettlementRateUnavailable,
                    match='merchant_settlement_rate_unavailable',
                ):
                    await create_merchant_settlement(
                        db,
                        merchant_id=merchant.id,
                        requested_by_id=owner.id,
                        amount_usdt=Decimal('10.00'),
                        trc20_address=VALID_ADDRESS,
                        idempotency_key=f'unavailable:{uuid.uuid4().hex}',
                    )
                assert (balance.available, balance.frozen) == before
                assert await db.scalar(
                    select(func.count(MerchantSettlement.id)).where(
                        MerchantSettlement.merchant_id == merchant.id
                    )
                ) == 0
                assert await db.scalar(
                    select(func.count(LedgerEntry.id)).where(
                        LedgerEntry.merchant_id == merchant.id
                    )
                ) == 0
                await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_exact_available_idempotency_and_quote_snapshot(monkeypatch):
    import app.services.settlements as settlements

    calls = 0

    async def live_quote():
        nonlocal calls
        calls += 1
        return _quote()

    monkeypatch.setattr(
        settlements,
        'get_strict_rolling_ask_quote',
        live_quote,
    )

    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, owner, _actor, balance = await _merchant_fixture(db)
                idem = f'exact:{uuid.uuid4().hex}'
                first = await create_merchant_settlement(
                    db,
                    merchant_id=merchant.id,
                    requested_by_id=owner.id,
                    amount_usdt=Decimal('95.00'),
                    trc20_address=VALID_ADDRESS,
                    idempotency_key=idem,
                )
                assert first.total_debit_rub == Decimal('10000.00')
                assert balance.available == Decimal('0.00')
                assert balance.frozen == Decimal('10000.00')
                assert first.rate_symbol == 'USDT/RUB'
                assert first.rate_side == 'ask'
                assert first.rate_source == 'rapira_live'
                assert first.rate_provider_field == 'askPrice'
                assert first.provider_timestamp is None
                assert first.fetched_at is not None
                assert first.freshness_basis == 'fetched_at'
                assert first.metadata_json['quote']['source'] == 'rapira_live'

                repeated = await create_merchant_settlement(
                    db,
                    merchant_id=merchant.id,
                    requested_by_id=owner.id,
                    amount_usdt=Decimal('95.00'),
                    trc20_address=VALID_ADDRESS,
                    idempotency_key=idem,
                )
                assert repeated.id == first.id
                assert calls == 1
                assert balance.available == Decimal('0.00')
                assert balance.frozen == Decimal('10000.00')

                with pytest.raises(
                    MerchantSettlementConflict,
                    match='merchant_settlement_idempotency_mismatch',
                ):
                    await create_merchant_settlement(
                        db,
                        merchant_id=merchant.id,
                        requested_by_id=owner.id,
                        amount_usdt=Decimal('94.00'),
                        trc20_address=VALID_ADDRESS,
                        idempotency_key=idem,
                    )
                assert calls == 1
                assert await db.scalar(
                    select(func.count(AuditLog.id)).where(
                        AuditLog.target_id == str(first.id),
                        AuditLog.action == 'merchant_settlement_requested',
                    )
                ) == 1
                await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_concurrent_requests_create_one_pending_and_one_hold(monkeypatch):
    import app.services.settlements as settlements

    async def live_quote():
        return _quote()

    monkeypatch.setattr(
        settlements,
        'get_strict_rolling_ask_quote',
        live_quote,
    )

    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as setup:
                merchant, owner, _actor, _balance = await _merchant_fixture(
                    setup,
                    available='100000.00',
                )
                merchant_id = merchant.id
                owner_id = owner.id
                await setup.commit()

            async def request(number: int):
                async with AsyncSession(engine, expire_on_commit=False) as db:
                    try:
                        settlement = await create_merchant_settlement(
                            db,
                            merchant_id=merchant_id,
                            requested_by_id=owner_id,
                            amount_usdt=Decimal('10.00'),
                            trc20_address=VALID_ADDRESS,
                            idempotency_key=(
                                f'concurrent:{number}:{uuid.uuid4().hex}'
                            ),
                        )
                        await db.commit()
                        return settlement.id
                    except Exception as exc:
                        await db.rollback()
                        return exc

            results = await asyncio.wait_for(
                asyncio.gather(request(1), request(2)),
                timeout=CONCURRENCY_TIMEOUT_SECONDS,
            )
            assert sum(isinstance(result, uuid.UUID) for result in results) == 1
            failures = [
                result
                for result in results
                if isinstance(result, Exception)
            ]
            assert len(failures) == 1
            assert isinstance(failures[0], MerchantSettlementConflict)
            assert str(failures[0]) == 'merchant_settlement_pending'

            async with AsyncSession(engine) as verify:
                balance = await verify.scalar(
                    select(Balance).where(
                        Balance.merchant_id == merchant_id,
                        Balance.currency == 'RUB',
                    )
                )
                assert balance.available == Decimal('98500.00')
                assert balance.frozen == Decimal('1500.00')
                assert await verify.scalar(
                    select(func.count(MerchantSettlement.id)).where(
                        MerchantSettlement.merchant_id == merchant_id,
                        MerchantSettlement.status == 'pending',
                    )
                ) == 1
                assert await verify.scalar(
                    select(func.count(LedgerEntry.id)).where(
                        LedgerEntry.merchant_id == merchant_id,
                        LedgerEntry.entry_type == 'hold',
                    )
                ) == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_completion_rejection_idempotency_and_tx_hash_uniqueness(monkeypatch):
    import app.services.settlements as settlements

    async def live_quote():
        return _quote()

    monkeypatch.setattr(
        settlements,
        'get_strict_rolling_ask_quote',
        live_quote,
    )

    async def scenario():
        engine = create_async_engine(_database_url())
        shared_tx = f'trc20-{uuid.uuid4().hex}'
        try:
            async with AsyncSession(engine, expire_on_commit=False) as setup:
                first_merchant, first_owner, actor, _ = (
                    await _merchant_fixture(setup, available='100000.00')
                )
                second_merchant, second_owner, _second_actor, _ = (
                    await _merchant_fixture(setup, available='100000.00')
                )
                await setup.commit()

            async with AsyncSession(engine, expire_on_commit=False) as db:
                rejected = await create_merchant_settlement(
                    db,
                    merchant_id=first_merchant.id,
                    requested_by_id=first_owner.id,
                    amount_usdt=Decimal('10.00'),
                    trc20_address=VALID_ADDRESS,
                    idempotency_key=f'reject:{uuid.uuid4().hex}',
                )
                await db.commit()
                await reject_merchant_settlement(
                    db,
                    settlement_id=rejected.id,
                    actor_id=actor.id,
                    reason='operator rejected',
                )
                await db.commit()
                repeated_rejection = await reject_merchant_settlement(
                    db,
                    settlement_id=rejected.id,
                    actor_id=actor.id,
                    reason='same terminal retry',
                )
                await db.commit()
                assert repeated_rejection.id == rejected.id

                completed = await create_merchant_settlement(
                    db,
                    merchant_id=first_merchant.id,
                    requested_by_id=first_owner.id,
                    amount_usdt=Decimal('10.00'),
                    trc20_address=VALID_ADDRESS,
                    idempotency_key=f'complete:{uuid.uuid4().hex}',
                )
                await db.commit()
                await complete_merchant_settlement(
                    db,
                    settlement_id=completed.id,
                    actor_id=actor.id,
                    tx_hash=shared_tx,
                )
                await db.commit()
                repeated_completion = await complete_merchant_settlement(
                    db,
                    settlement_id=completed.id,
                    actor_id=actor.id,
                    tx_hash=shared_tx,
                )
                await db.commit()
                assert repeated_completion.id == completed.id
                with pytest.raises(
                    MerchantSettlementConflict,
                    match='merchant_settlement_already_completed',
                ):
                    await complete_merchant_settlement(
                        db,
                        settlement_id=completed.id,
                        actor_id=actor.id,
                        tx_hash=f'different-{uuid.uuid4().hex}',
                    )
                await db.rollback()

                duplicate = await create_merchant_settlement(
                    db,
                    merchant_id=second_merchant.id,
                    requested_by_id=second_owner.id,
                    amount_usdt=Decimal('10.00'),
                    trc20_address=VALID_ADDRESS,
                    idempotency_key=f'duplicate:{uuid.uuid4().hex}',
                )
                await db.commit()
                with pytest.raises(
                    MerchantSettlementConflict,
                    match='merchant_settlement_tx_hash_duplicate',
                ):
                    await complete_merchant_settlement(
                        db,
                        settlement_id=duplicate.id,
                        actor_id=actor.id,
                        tx_hash=shared_tx,
                    )
                await db.rollback()

            async with AsyncSession(engine) as verify:
                first_balance = await verify.scalar(
                    select(Balance).where(
                        Balance.merchant_id == first_merchant.id,
                        Balance.currency == 'RUB',
                    )
                )
                second_balance = await verify.scalar(
                    select(Balance).where(
                        Balance.merchant_id == second_merchant.id,
                        Balance.currency == 'RUB',
                    )
                )
                assert first_balance.available == Decimal('98500.00')
                assert first_balance.frozen == Decimal('0.00')
                assert second_balance.available == Decimal('98500.00')
                assert second_balance.frozen == Decimal('1500.00')
                assert await verify.scalar(
                    select(func.count(MerchantSettlement.id)).where(
                        MerchantSettlement.network == 'TRC20',
                        MerchantSettlement.tx_hash == shared_tx,
                        MerchantSettlement.status == 'completed',
                    )
                ) == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_route_returns_503_when_strict_rate_is_unavailable(monkeypatch):
    import app.services.settlements as settlements

    async def unavailable():
        raise RollingRateUnavailable('rolling_rate_unavailable')

    monkeypatch.setattr(
        settlements,
        'get_strict_rolling_ask_quote',
        unavailable,
    )

    async def scenario():
        engine = create_async_engine(_database_url())
        password = f'Password-{uuid.uuid4().hex}'
        try:
            await application_engine.dispose(close=False)
            async with AsyncSession(engine, expire_on_commit=False) as setup:
                merchant, owner, _actor, _balance = await _merchant_fixture(
                    setup
                )
                owner.password_hash = hash_password(password)
                merchant_id = merchant.id
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
                login_page = await client.get('/merchant/login')
                csrf_match = re.search(
                    r'name="csrf_token" value="([^"]+)"',
                    login_page.text,
                )
                assert csrf_match
                login = await client.post(
                    '/merchant/login',
                    data={
                        'csrf_token': csrf_match.group(1),
                        'email': owner.email,
                        'password': password,
                        'otp': '',
                    },
                )
                assert login.status_code == 303
                cabinet = await client.get('/merchant/cabinet/tradespace/settlements')
                assert cabinet.status_code == 200
                assert 'Текущий курс временно недоступен' in cabinet.text
                assert 'action="/merchant/cabinet/settlements/request"' not in cabinet.text
                response = await client.post(
                    '/merchant/cabinet/settlements/request',
                    data={
                        'csrf_token': client.cookies[
                            'processing_merchant_csrf'
                        ],
                        'amount_usdt': '10.00',
                        'trc20_address': VALID_ADDRESS,
                        'idempotency_key': f'route:{uuid.uuid4().hex}',
                    },
                )
                assert response.status_code == 503
                assert response.json()['code'] == (
                    'merchant_settlement_rate_unavailable'
                )

            async with AsyncSession(engine) as verify:
                balance = await verify.scalar(
                    select(Balance).where(
                        Balance.merchant_id == merchant_id,
                        Balance.currency == 'RUB',
                    )
                )
                assert balance.available == Decimal('10000.00')
                assert balance.frozen == Decimal('0.00')
        finally:
            await application_engine.dispose()
            await engine.dispose()

    asyncio.run(scenario())


def test_old_fetched_at_is_rejected_before_balance_lock(monkeypatch):
    import app.services.settlements as settlements

    async def stale_quote():
        return _quote(
            fetched_at=(
                datetime.now(timezone.utc)
                - timedelta(
                    seconds=settings.ROLLING_RAPIRA_MAX_AGE_SECONDS + 1
                )
            )
        )

    from app.core.config import settings

    monkeypatch.setattr(
        settlements,
        'get_strict_rolling_ask_quote',
        stale_quote,
    )

    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, owner, _actor, balance = await _merchant_fixture(db)
                before = (balance.available, balance.frozen)
                with pytest.raises(MerchantSettlementRateUnavailable):
                    await create_merchant_settlement(
                        db,
                        merchant_id=merchant.id,
                        requested_by_id=owner.id,
                        amount_usdt=Decimal('10.00'),
                        trc20_address=VALID_ADDRESS,
                        idempotency_key=f'stale:{uuid.uuid4().hex}',
                    )
                assert (balance.available, balance.frozen) == before
                await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())
