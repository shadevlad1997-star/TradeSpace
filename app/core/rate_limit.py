import asyncio
import logging
import time

from redis.asyncio import Redis
from starlette.requests import Request

from app.core.config import settings
from app.core.client_ip import client_ip
from app.core.metrics import metrics_registry, normalize_path
from app.core.security import sha256_text


_redis = None
_redis_loop = None
security_logger = logging.getLogger('app.security')


def _redis_for_current_loop():
    global _redis, _redis_loop
    loop = asyncio.get_running_loop()
    if _redis is None or _redis_loop is not loop:
        _redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
        _redis_loop = loop
    return _redis


def _parse_limit(value: str) -> tuple[int, int]:
    count, period = value.split('/', 1)
    period = period.strip().lower()
    seconds = {
        'second': 1,
        'seconds': 1,
        'sec': 1,
        'minute': 60,
        'minutes': 60,
        'min': 60,
        'hour': 3600,
        'hours': 3600,
    }.get(period, 60)
    return max(1, int(count)), seconds


async def hit_rate_limit(scope: str, request: Request, identifier: str, limit: str) -> int | None:
    max_hits, window = _parse_limit(limit)
    ip = client_ip(request)
    ident = sha256_text(identifier.strip().lower() or 'anonymous')[:16]
    bucket = int(time.time()) // window
    key = f'rl:{scope}:{ip}:{ident}:{bucket}'
    try:
        redis = _redis_for_current_loop()
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, window + 5)
        if count > max_hits:
            return window
    except Exception as exc:
        metrics_registry.increment('processing_platform_rate_limit_backend_errors_total', {'scope': scope, 'path': normalize_path(request.url.path)})
        security_logger.warning(
            'rate_limit_backend_error',
            extra={
                'request_id': getattr(request.state, 'request_id', ''),
                'path': request.url.path,
                'client_ip': ip,
                'scope': scope,
                'reason': str(exc)[:500],
            },
        )
        if settings.is_production and settings.RATE_LIMIT_FAIL_CLOSED_IN_PRODUCTION:
            return settings.RATE_LIMIT_REDIS_FAILURE_RETRY_AFTER_SECONDS
    return None
