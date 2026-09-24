import logging
import time
from typing import Any

from redis.asyncio import Redis

from app.core.config import settings


security_logger = logging.getLogger('app.security')
_redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
_memory_locks: dict[str, float] = {}


def _user_id(user: Any) -> str:
    return str(getattr(user, 'id', '') or '')


def _scope_config(scope: str) -> tuple[int, int]:
    if scope == '2fa':
        return settings.TWOFA_FAILURE_LIMIT, settings.TWOFA_LOCKOUT_SECONDS
    return settings.AUTH_FAILURE_LIMIT, settings.AUTH_LOCKOUT_SECONDS


def get_auth_lock_key(user: Any, scope: str) -> str:
    return f'auth-lock:{scope}:{_user_id(user)}'


def _memory_ttl(key: str) -> int | None:
    expires_at = _memory_locks.get(key)
    if not expires_at:
        return None
    remaining = int(expires_at - time.time())
    if remaining <= 0:
        _memory_locks.pop(key, None)
        return None
    return remaining


async def get_auth_lock_ttl(user: Any, scope: str) -> int | None:
    key = get_auth_lock_key(user, scope)
    try:
        ttl = await _redis.ttl(key)
        if ttl and ttl > 0:
            return int(ttl)
        if ttl == -1:
            _, seconds = _scope_config(scope)
            await _redis.expire(key, seconds)
            return seconds
    except Exception as exc:
        security_logger.warning('auth_lock_redis_unavailable', extra={'scope': scope, 'reason': str(exc)[:300]})
    return _memory_ttl(key)


async def is_auth_temporarily_locked(user: Any, scope: str) -> bool:
    return bool(await get_auth_lock_ttl(user, scope))


async def maybe_lock_auth_flow(user: Any, scope: str) -> bool:
    limit, seconds = _scope_config(scope)
    count = int(getattr(user, 'failed_login_count', 0) or 0)
    if count < limit:
        return False

    key = get_auth_lock_key(user, scope)
    try:
        await _redis.set(key, '1', ex=seconds)
    except Exception as exc:
        security_logger.warning('auth_lock_redis_unavailable', extra={'scope': scope, 'reason': str(exc)[:300]})
        _memory_locks[key] = time.time() + seconds
    return True


async def clear_auth_locks(user: Any) -> None:
    keys = [get_auth_lock_key(user, 'login'), get_auth_lock_key(user, '2fa')]
    try:
        await _redis.delete(*keys)
    except Exception as exc:
        security_logger.warning('auth_lock_redis_unavailable', extra={'scope': 'clear', 'reason': str(exc)[:300]})
    for key in keys:
        _memory_locks.pop(key, None)


async def record_auth_failure(user: Any, reason: str, scope: str) -> bool:
    user.failed_login_count = int(getattr(user, 'failed_login_count', 0) or 0) + 1
    return await maybe_lock_auth_flow(user, scope)


async def record_auth_success(user: Any, scope: str) -> None:
    user.failed_login_count = 0
    await clear_auth_locks(user)
