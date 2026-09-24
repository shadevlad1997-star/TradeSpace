import asyncio
import hashlib
import uuid
from pathlib import Path

import httpx
import pytest
from redis.asyncio import Redis

from app.core.config import settings
from app.core.middleware import SimpleRedisRateLimitMiddleware
from app.main import app


def _require_test_environment() -> None:
    if settings.ENV.lower() != 'test':
        pytest.skip('rate-limit isolation tests require ENV=test')


async def _version_requests(
    count: int,
    *,
    client_ip: str | None = None,
) -> list[httpx.Response]:
    kwargs = {'app': app, 'raise_app_exceptions': False}
    if client_ip:
        kwargs['client'] = (client_ip, 123)
    transport = httpx.ASGITransport(**kwargs)
    async with httpx.AsyncClient(
        transport=transport,
        base_url='https://localhost',
    ) as client:
        return [await client.get('/version') for _ in range(count)]


def _derived_client_ip(namespace: str, label: str) -> str:
    digest = hashlib.sha256(f'{namespace}:{label}'.encode()).hexdigest()
    groups = [digest[index:index + 4] for index in range(0, 24, 4)]
    return '2001:db8:' + ':'.join(groups)


def test_test_harness_does_not_require_redis_for_migration_only_tests():
    source = (Path(__file__).parent / 'conftest.py').read_text(encoding='utf-8')
    assert 'redis.asyncio' not in source
    assert 'Redis.from_url' not in source
    assert 'REDIS_URL' not in source


def test_01_rate_limit_state_near_threshold_is_scoped_to_one_test_case():
    _require_test_environment()
    responses = asyncio.run(_version_requests(119))
    assert all(response.status_code == 200 for response in responses)


def test_02_next_test_starts_without_an_unexpected_429():
    _require_test_environment()
    response = asyncio.run(_version_requests(1))[0]
    assert response.status_code == 200


def test_global_rate_limiter_still_allows_120_then_returns_429(
    isolated_rate_limit_ip,
):
    _require_test_environment()
    middleware = next(
        item
        for item in app.user_middleware
        if item.cls is SimpleRedisRateLimitMiddleware
    )
    assert middleware.kwargs == {'limit': 120, 'window': 60}

    async def scenario():
        first_client = await _version_requests(60)
        second_client = await _version_requests(61)
        return first_client + second_client

    responses = asyncio.run(scenario())
    assert all(response.status_code == 200 for response in responses[:120])
    assert responses[120].status_code == 429
    assert responses[120].json() == {'detail': 'rate limit exceeded'}


def test_rate_limit_namespaces_are_separate_and_unrelated_keys_are_preserved(
    isolated_rate_limit_ip,
):
    _require_test_environment()
    first_ip = _derived_client_ip(isolated_rate_limit_ip, 'first')
    second_ip = _derived_client_ip(isolated_rate_limit_ip, 'second')
    unrelated_key = f'test-rate-limit-unrelated:{uuid.uuid4().hex}'

    async def scenario():
        redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
        try:
            await redis.set(unrelated_key, 'keep-me', ex=60)

            first = await _version_requests(121, client_ip=first_ip)
            second = await _version_requests(1, client_ip=second_ip)
            assert all(response.status_code == 200 for response in first[:120])
            assert first[120].status_code == 429
            assert second[0].status_code == 200
            assert await redis.get(unrelated_key) == 'keep-me'
        finally:
            await redis.delete(unrelated_key)
            await redis.aclose()

    asyncio.run(scenario())
