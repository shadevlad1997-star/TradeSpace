import asyncio
import time
import re
import logging
import os
import uuid
import secrets
from urllib.parse import parse_qs, urlsplit

from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse, RedirectResponse
from redis.asyncio import Redis
from app.core.config import settings
from app.core.client_ip import client_ip_from_scope
from app.core.metrics import metrics_registry, normalize_path
from app.core.session import REALM_COOKIE_NAMES, REALM_CSRF_COOKIE_NAMES


BAD_PATH = re.compile(r'(\.\.|%2e%2e|/etc/passwd|/proc/|cmd=|powershell|bash\s+-c|;|\|\||&&|`)', re.I)
request_logger = logging.getLogger('app.requests')
security_logger = logging.getLogger('app.security')


class RequestObservabilityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get('type') != 'http':
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in (scope.get('headers') or [])}
        request_id_header = settings.REQUEST_ID_HEADER.lower().encode()
        request_id = headers.get(request_id_header, b'').decode('utf-8', errors='ignore').strip()
        if not request_id:
            request_id = uuid.uuid4().hex
        scope['request_id'] = request_id

        start = time.perf_counter()
        status_code = 500
        redirect_target = ''
        response_session_cookie = False
        session_cookie_names = set(REALM_COOKIE_NAMES.values()) | {settings.SESSION_COOKIE_NAME}

        async def send_wrapper(message):
            nonlocal status_code, redirect_target, response_session_cookie
            if message.get('type') == 'http.response.start':
                status_code = int(message.get('status') or 500)
                raw_headers = list(message.get('headers') or [])
                for name, value in raw_headers:
                    lower_name = name.lower()
                    if lower_name == b'location':
                        redirect_target = value.decode('utf-8', errors='ignore').split('?', 1)[0][:300]
                    elif lower_name == b'set-cookie':
                        cookie_name = value.split(b'=', 1)[0].decode('latin-1', errors='ignore').strip()
                        if cookie_name in session_cookie_names:
                            response_session_cookie = True
                duration_ms = (time.perf_counter() - start) * 1000
                raw_headers = _set_header(raw_headers, settings.REQUEST_ID_HEADER.lower().encode(), request_id.encode())
                raw_headers = _set_header(raw_headers, b'x-response-time-ms', f'{duration_ms:.2f}'.encode())
                message['headers'] = raw_headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_seconds = time.perf_counter() - start
            method = scope.get('method', 'GET')
            raw_path = scope.get('auth_original_path') or scope.get('path', '/') or '/'
            route_path = getattr(scope.get('route'), 'path', None) or normalize_path(raw_path)
            metrics_registry.observe_http(method=method, path=route_path, status_code=status_code, duration_seconds=duration_seconds)
            client_ip = client_ip_from_scope(scope)
            session = scope.get('session') or {}
            session_state = scope.get('session_observability') or {}
            session_reason = scope.get('session_event_reason') or session_state.get('reason') or ''
            polling_request = raw_path.endswith('/cabinet/rates/usdt-rub') or b'x-session-view' in headers
            request_logger.info(
                'http_request_completed',
                extra={
                    'request_id': request_id,
                    'method': method,
                    'path': raw_path,
                    'route': route_path,
                    'status_code': status_code,
                    'duration_ms': round(duration_seconds * 1000, 2),
                    'client_ip': client_ip,
                    'current_user_id': str(session.get('user_id') or session_state.get('initial_user_id') or ''),
                    'current_role': str(session.get('role') or session_state.get('initial_role') or ''),
                    'auth_realm': str(scope.get('auth_realm') or session_state.get('realm') or ''),
                    'session_fingerprint': str(session_state.get('fingerprint') or ''),
                    'session_result_fingerprint': str(session_state.get('result_fingerprint') or ''),
                    'session_loaded': bool(session_state.get('loaded')),
                    'session_changed': bool(session_state.get('changed')),
                    'session_cleared': bool(session_state.get('cleared')),
                    'session_regenerated': bool(session_state.get('regenerated')),
                    'session_set_cookie': bool(session_state.get('set_cookie') or response_session_cookie),
                    'legacy_session_cookie_cleared': bool(session_state.get('legacy_cookie_cleared')),
                    'redirect_target': redirect_target,
                    'polling_request': polling_request,
                    'auth_failure_reason': str(session_reason)[:120],
                    'worker_id': os.getenv('HOSTNAME') or os.getenv('COMPUTERNAME') or 'unknown',
                },
            )


def _set_header(headers: list[tuple[bytes, bytes]], name: bytes, value: bytes) -> list[tuple[bytes, bytes]]:
    lname = name.lower()
    headers = [(k, v) for (k, v) in headers if k.lower() != lname]
    headers.append((name, value))
    return headers


class SecurityHeadersMiddleware:
    """Safe ASGI middleware: only changes response-start headers, never touches body.

    Previous builds used a response middleware that could leave an old Content-Length
    after Starlette/session cookies changed the response. That produced:
    RuntimeError: Response content longer than Content-Length.
    This implementation removes Content-Length at the raw ASGI-header level before
    the response reaches Uvicorn.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get('type') != 'http':
            await self.app(scope, receive, send)
            return

        nonce = secrets.token_urlsafe(18)
        scope['csp_nonce'] = nonce

        async def send_wrapper(message):
            if message.get('type') == 'http.response.start':
                raw_headers = list(message.get('headers') or [])
                raw_headers = [(k, v) for (k, v) in raw_headers if k.lower() != b'content-length']
                raw_headers = _set_header(raw_headers, b'x-content-type-options', b'nosniff')
                raw_headers = _set_header(raw_headers, b'x-frame-options', b'DENY')
                raw_headers = _set_header(raw_headers, b'referrer-policy', b'strict-origin-when-cross-origin')
                raw_headers = _set_header(raw_headers, b'permissions-policy', b'camera=(), microphone=(), geolocation=()')
                path = scope.get('path', '') or ''
                if path in {'/docs', '/redoc'}:
                    csp = "default-src 'self'; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: https://fastapi.tiangolo.com; font-src 'self' https://cdn.jsdelivr.net data:; connect-src 'self'"
                else:
                    csp = f"default-src 'self'; style-src 'self' 'nonce-{nonce}' https://cdn.jsdelivr.net; script-src 'self' 'nonce-{nonce}' https://cdn.jsdelivr.net; img-src 'self' data:; font-src 'self' https://cdn.jsdelivr.net data:; connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'"
                raw_headers = _set_header(raw_headers, b'content-security-policy', csp.encode())
                if settings.HSTS_ENABLED:
                    hsts = f'max-age={settings.HSTS_MAX_AGE_SECONDS}; includeSubDomains'
                    raw_headers = _set_header(raw_headers, b'strict-transport-security', hsts.encode())
                message['headers'] = raw_headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


class SuspiciousInputMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get('type') != 'http':
            await self.app(scope, receive, send)
            return
        path = scope.get('path', '') or ''
        query = (scope.get('query_string') or b'').decode('utf-8', errors='ignore')
        raw = path + '?' + query
        if BAD_PATH.search(raw):
            metrics_registry.increment('processing_platform_suspicious_requests_total', {'reason': 'bad_path'})
            ip = client_ip_from_scope(scope)
            security_logger.warning(
                'suspicious_request_blocked',
                extra={
                    'request_id': scope.get('request_id', ''),
                    'path': path,
                    'client_ip': ip,
                    'reason': 'bad_path',
                },
            )
            response = JSONResponse({'detail': 'blocked suspicious request'}, status_code=400)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class BodySizeLimitMiddleware:
    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope.get('type') != 'http':
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get('headers') or [])
        content_length = headers.get(b'content-length')
        if content_length:
            try:
                if int(content_length.decode()) > self.max_bytes:
                    response = JSONResponse({'detail': 'request body too large'}, status_code=413)
                    await response(scope, receive, send)
                    return
            except ValueError:
                response = JSONResponse({'detail': 'bad content-length'}, status_code=400)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class CSRFOriginMiddleware:
    """Origin/Referer plus double-submit token validation for browser forms."""

    SAFE_METHODS = {'GET', 'HEAD', 'OPTIONS'}
    UNSAFE_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}
    COOKIE_NAME = 'processing_csrf'

    def __init__(self, app):
        self.app = app
        self.trusted = set(settings.csrf_origins)

    def _cookie_token(self, headers: dict[bytes, bytes], cookie_name: str) -> str | None:
        cookie = headers.get(b'cookie', b'').decode('utf-8', errors='ignore')
        for chunk in cookie.split(';'):
            name, _, value = chunk.strip().partition('=')
            if name == cookie_name and value:
                return value
        return None

    @staticmethod
    def _cookie_name(scope, path: str, method: str) -> str | None:
        # Polling is read-only and must never create or rotate browser state.
        if method in CSRFOriginMiddleware.SAFE_METHODS and path.endswith('/cabinet/rates/usdt-rub'):
            return None
        csrf_realm = scope.get('csrf_realm')
        if csrf_realm in REALM_CSRF_COOKIE_NAMES:
            return REALM_CSRF_COOKIE_NAMES[csrf_realm]
        if scope.get('auth_sessions') is not None:
            if path == '/login' or (method in CSRFOriginMiddleware.UNSAFE_METHODS and not path.startswith('/api/')):
                return REALM_CSRF_COOKIE_NAMES['login']
            return None
        if path == '/login' or path == '/logout' or path.startswith('/cabinet'):
            return CSRFOriginMiddleware.COOKIE_NAME
        return None

    @staticmethod
    def _set_csrf_cookie(response, cookie_name: str, token: str) -> None:
        response.set_cookie(
            cookie_name,
            token,
            max_age=7200,
            path='/',
            httponly=True,
            samesite='lax',
            secure=settings.is_production,
        )

    async def _read_body(self, receive) -> bytes:
        chunks: list[bytes] = []
        more_body = True
        while more_body:
            message = await receive()
            chunks.append(message.get('body', b''))
            more_body = bool(message.get('more_body', False))
        return b''.join(chunks)

    def _receive_with_body(self, body: bytes):
        sent = False

        async def inner():
            nonlocal sent
            if not sent:
                sent = True
                return {'type': 'http.request', 'body': body, 'more_body': False}
            return {'type': 'http.request', 'body': b'', 'more_body': False}

        return inner

    async def __call__(self, scope, receive, send):
        if scope.get('type') != 'http':
            await self.app(scope, receive, send)
            return
        method = scope.get('method', 'GET').upper()
        path = scope.get('path', '')
        headers = {k.lower(): v for k, v in (scope.get('headers') or [])}
        cookie_name = self._cookie_name(scope, path, method)
        existing_token = self._cookie_token(headers, cookie_name) if cookie_name else None
        token = existing_token or (secrets.token_urlsafe(32) if cookie_name else '')
        token_created = bool(cookie_name and not existing_token)
        scope['csrf_token'] = token

        if method in self.UNSAFE_METHODS and not path.startswith('/api/'):
            origin = headers.get(b'origin')
            referer = headers.get(b'referer')
            candidate = None
            if origin:
                candidate = origin.decode('utf-8', errors='ignore').rstrip('/')
            elif referer:
                parsed = urlsplit(referer.decode('utf-8', errors='ignore'))
                if parsed.scheme and parsed.netloc:
                    candidate = f'{parsed.scheme}://{parsed.netloc}'.rstrip('/')
            if candidate and candidate not in self.trusted:
                metrics_registry.increment('processing_platform_csrf_blocks_total', {'origin': candidate[:80]})
                ip = client_ip_from_scope(scope)
                security_logger.warning(
                    'csrf_origin_blocked',
                    extra={'request_id': scope.get('request_id', ''), 'path': path, 'origin': candidate, 'client_ip': ip},
                )
                response = JSONResponse({'detail': 'csrf origin blocked'}, status_code=403)
                if token_created:
                    self._set_csrf_cookie(response, cookie_name, token)
                await response(scope, receive, send)
                return

            body = await self._read_body(receive)
            receive = self._receive_with_body(body)
            submitted = headers.get(b'x-csrf-token', b'').decode('utf-8', errors='ignore')
            content_type = headers.get(b'content-type', b'')
            if not submitted and b'application/x-www-form-urlencoded' in content_type:
                submitted = (parse_qs(body.decode('utf-8', errors='ignore')).get('csrf_token') or [''])[0]
            if not submitted and b'multipart/form-data' in content_type:
                match = re.search(br'name="csrf_token"\r?\n\r?\n([^\r\n]*)', body)
                if match:
                    submitted = match.group(1).decode('utf-8', errors='ignore')
            if not submitted or not secrets.compare_digest(submitted, token):
                metrics_registry.increment('processing_platform_csrf_blocks_total', {'origin': 'token'})
                ip = client_ip_from_scope(scope)
                security_logger.warning(
                    'csrf_token_blocked',
                    extra={'request_id': scope.get('request_id', ''), 'path': path, 'client_ip': ip},
                )
                accept_header = headers.get(b'accept', b'').decode('utf-8', errors='ignore').lower()
                if path == '/login' and 'text/html' in accept_header:
                    response = RedirectResponse('/login?error=Сессия формы устарела. Введите данные еще раз.', status_code=303)
                    self._set_csrf_cookie(response, cookie_name, token)
                    await response(scope, receive, send)
                    return
                response = JSONResponse({'detail': 'csrf token invalid'}, status_code=403)
                if token_created:
                    self._set_csrf_cookie(response, cookie_name, token)
                await response(scope, receive, send)
                return

        async def send_wrapper(message):
            if message.get('type') == 'http.response.start' and token_created:
                mutable = MutableHeaders(scope=message)
                secure = settings.is_production
                cookie = f'{cookie_name}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=7200'
                if secure:
                    cookie += '; Secure'
                mutable.append('set-cookie', cookie)
            if message.get('type') == 'http.response.start' and scope.get('auth_sessions') is not None:
                legacy_token = self._cookie_token(headers, self.COOKIE_NAME)
                if legacy_token:
                    mutable = MutableHeaders(scope=message)
                    cookie = (
                        f'{self.COOKIE_NAME}=null; Path=/; HttpOnly; SameSite=Lax; Max-Age=0; '
                        'Expires=Thu, 01 Jan 1970 00:00:00 GMT'
                    )
                    if settings.is_production:
                        cookie += '; Secure'
                    mutable.append('set-cookie', cookie)
            await send(message)

        await self.app(scope, receive, send_wrapper)


class SimpleRedisRateLimitMiddleware:
    def __init__(self, app, limit=120, window=60):
        self.app = app
        self.limit = limit
        self.window = window
        self.redis = None
        self.redis_loop = None

    def _redis_for_current_loop(self):
        loop = asyncio.get_running_loop()
        if self.redis is None or self.redis_loop is not loop:
            self.redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
            self.redis_loop = loop
        return self.redis

    async def __call__(self, scope, receive, send):
        if scope.get('type') != 'http':
            await self.app(scope, receive, send)
            return
        ip = client_ip_from_scope(scope)
        key = f'rl:{ip}:{int(time.time()) // self.window}'
        try:
            redis = self._redis_for_current_loop()
            count = await redis.incr(key)
            if count == 1:
                await redis.expire(key, self.window + 5)
            if count > self.limit:
                metrics_registry.increment('processing_platform_rate_limited_requests_total', {'path': normalize_path(scope.get('path', '/'))})
                security_logger.warning(
                    'rate_limit_exceeded',
                    extra={
                        'request_id': scope.get('request_id', ''),
                        'path': scope.get('path', '/'),
                        'client_ip': ip,
                        'limit': self.limit,
                        'window': self.window,
                    },
                )
                response = JSONResponse({'detail': 'rate limit exceeded'}, status_code=429)
                await response(scope, receive, send)
                return
        except Exception as exc:
            metrics_registry.increment('processing_platform_rate_limit_backend_errors_total', {'scope': 'global', 'path': normalize_path(scope.get('path', '/'))})
            security_logger.warning(
                'rate_limit_backend_error',
                extra={
                    'request_id': scope.get('request_id', ''),
                    'path': scope.get('path', '/'),
                    'client_ip': ip,
                    'scope': 'global',
                    'reason': str(exc)[:500],
                },
            )
            protected_path = (scope.get('path') or '').startswith(('/login', '/api/v1/auth', '/api/v1/merchant', '/api/v1/admin', '/cabinet'))
            if settings.is_production and settings.RATE_LIMIT_FAIL_CLOSED_IN_PRODUCTION and protected_path:
                response = JSONResponse(
                    {'detail': 'rate limiter temporarily unavailable'},
                    status_code=503,
                    headers={'Retry-After': str(settings.RATE_LIMIT_REDIS_FAILURE_RETRY_AFTER_SECONDS)},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
