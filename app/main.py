import orjson
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import FileResponse, ORJSONResponse, Response
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis
from sqlalchemy import select, text
from starlette.middleware.trustedhost import TrustedHostMiddleware
from app.core.config import settings
from app.core.api_errors import MerchantApiError
from app.core.branding import get_branding
from app.core.enums import Role
from app.core.logging import configure_logging
from app.core.metrics import metrics_registry
from app.core.middleware import BodySizeLimitMiddleware, CSRFOriginMiddleware, RequestObservabilityMiddleware, SecurityHeadersMiddleware, SimpleRedisRateLimitMiddleware, SuspiciousInputMiddleware
from app.core.session import RealmSessionMiddleware
from app.db.session import AsyncSessionLocal, engine
from app.models import User
from app.api.v1.router import api_router
from app.web.routes import router as web_router

configure_logging()
brand = get_branding()

class CustomORJSONResponse(ORJSONResponse):
    def render(self, content) -> bytes:
        return orjson.dumps(content, option=orjson.OPT_NON_STR_KEYS | orjson.OPT_SERIALIZE_NUMPY)

app=FastAPI(
    title=brand.api_docs_title,
    version=settings.APP_VERSION,
    default_response_class=CustomORJSONResponse,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.exception_handler(MerchantApiError)
async def merchant_api_error_handler(
    request: Request,
    exc: MerchantApiError,
) -> ORJSONResponse:
    error = {
        'code': exc.code,
        'message': exc.message,
        'request_id': str(request.scope.get('request_id') or ''),
    }
    if exc.details:
        error['details'] = exc.details
    return CustomORJSONResponse(
        status_code=exc.status_code,
        content={'error': error},
    )


if settings.trusted_hosts and '*' not in settings.trusted_hosts:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_list, allow_credentials=True, allow_methods=['GET','POST','PUT','PATCH','DELETE'], allow_headers=['*'])
app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.MAX_REQUEST_BODY_BYTES)
app.add_middleware(CSRFOriginMiddleware)
app.add_middleware(
    RealmSessionMiddleware,
    secret_key=settings.SECRET_KEY,
    same_site='lax',
    https_only=not settings.DEBUG,
    max_age=settings.SESSION_COOKIE_MAX_AGE_SECONDS,
    legacy_session_cookie=settings.SESSION_COOKIE_NAME,
)
app.add_middleware(SuspiciousInputMiddleware)
app.add_middleware(SimpleRedisRateLimitMiddleware, limit=120, window=60)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestObservabilityMiddleware)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(api_router)
app.include_router(web_router)


async def require_docs_superadmin(request: Request) -> None:
    user_id = request.session.get('user_id')
    if not user_id:
        raise HTTPException(404, 'not found')
    async with AsyncSessionLocal() as db:
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user or user.role != Role.superadmin.value or not user.is_active or user.is_locked:
        raise HTTPException(404, 'not found')


@app.get('/docs', include_in_schema=False)
async def protected_docs(request: Request):
    if not settings.DOCS_ENABLED:
        raise HTTPException(404, 'not found')
    await require_docs_superadmin(request)
    return get_swagger_ui_html(
        openapi_url='/openapi.json',
        title=f'{brand.api_docs_title} - Swagger',
        swagger_js_url='/static/swagger-ui-bundle.js',
        swagger_css_url='/static/swagger-ui.css',
    )


@app.get('/redoc', include_in_schema=False)
async def protected_redoc(request: Request):
    if not settings.DOCS_ENABLED:
        raise HTTPException(404, 'not found')
    await require_docs_superadmin(request)
    return get_redoc_html(openapi_url='/openapi.json', title=f'{brand.api_docs_title} - ReDoc')


@app.get('/openapi.json', include_in_schema=False)
async def protected_openapi(request: Request):
    if not settings.OPENAPI_ENABLED:
        raise HTTPException(404, 'not found')
    await require_docs_superadmin(request)
    return CustomORJSONResponse(app.openapi())

@app.get('/health')
async def health():
    return {'status': 'ok', 'version': settings.APP_VERSION}


@app.get('/version')
async def version():
    return {'version': settings.APP_VERSION}

@app.get('/ready')
async def ready():
    checks = {'database': 'unknown', 'redis': 'unknown'}
    status_code = 200
    try:
        async with engine.connect() as conn:
            await conn.execute(text('SELECT 1'))
        from app.services.integration_modes import verify_environment
        async with AsyncSessionLocal() as db:
            await verify_environment(db)
        checks['database'] = 'ok'
    except Exception:
        checks['database'] = 'failed'
        status_code = 503
    redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        await redis.ping()
        checks['redis'] = 'ok'
    except Exception:
        checks['redis'] = 'failed'
        status_code = 503
    finally:
        await redis.aclose()
    payload = {'status': 'ready' if status_code == 200 else 'not_ready', 'checks': checks}
    return CustomORJSONResponse(payload, status_code=status_code)

@app.get('/metrics', include_in_schema=False)
async def metrics(request: Request):
    if not settings.METRICS_ENABLED:
        raise HTTPException(404, 'not found')
    if settings.is_production and not settings.METRICS_TOKEN:
        raise HTTPException(403, 'metrics token required')
    if settings.METRICS_TOKEN:
        auth = request.headers.get('authorization', '')
        token = request.headers.get('x-metrics-token', '')
        if auth != f'Bearer {settings.METRICS_TOKEN}' and token != settings.METRICS_TOKEN:
            raise HTTPException(403, 'metrics token required')
    return Response(metrics_registry.render(), media_type='text/plain; version=0.0.4; charset=utf-8')

@app.get('/favicon.ico', include_in_schema=False)
async def favicon():
    if brand.favicon.startswith('/static/'):
        asset_path = Path('app') / brand.favicon.lstrip('/')
        if asset_path.exists() and asset_path.is_file():
            media_type = 'image/svg+xml' if asset_path.suffix.lower() == '.svg' else None
            return FileResponse(asset_path, media_type=media_type)
    return Response(status_code=204)
