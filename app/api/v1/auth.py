from datetime import datetime, timedelta, timezone
import pyotp
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_db
from app.models import RefreshToken, User
from app.schemas.common import LoginIn, RefreshIn, TokenOut
from app.core.rate_limit import hit_rate_limit
from app.core.security import auth_state_marker, decode_token, decrypt_secret, sha256_text, verify_password, create_token
from app.core.config import settings
from app.core.client_ip import client_ip
from app.core.auth_hardening import is_auth_temporarily_locked, record_auth_failure, record_auth_success
from app.api.deps import STAFF_2FA_ROLES
from app.services.audit import audit
router = APIRouter(prefix='/auth', tags=['auth'])

def _client_ip(request: Request) -> str:
    return client_ip(request)

@router.post('/login', response_model=TokenOut)
async def login(data: LoginIn, request: Request, db: AsyncSession=Depends(get_db)):
    retry_after = await hit_rate_limit('api-login', request, data.email, settings.LOGIN_RATE_LIMIT)
    if retry_after:
        raise HTTPException(429, 'too many login attempts')
    user = (await db.execute(select(User).where(User.email==data.email))).scalar_one_or_none()
    ip = _client_ip(request)
    if user and await is_auth_temporarily_locked(user, 'login'):
        await audit(db, 'account_locked', 'user', actor_id=user.id, ip=ip, details={'scope': 'login'})
        await db.commit()
        raise HTTPException(429, 'too many login attempts')
    password_ok = bool(user and user.is_active and not user.is_locked and verify_password(data.password, user.password_hash))
    if not password_ok:
        if user and user.is_active and not user.is_locked:
            locked = await record_auth_failure(user, 'password', 'login')
            if locked:
                await audit(db, 'account_locked', 'user', actor_id=user.id, ip=ip, details={'scope': 'login'})
        await audit(db, 'failed_login', 'user', target_id=data.email, ip=ip)
        await db.commit()
        raise HTTPException(401, 'wrong email or password')
    if user.twofa_enabled:
        if await is_auth_temporarily_locked(user, '2fa'):
            await audit(db, 'account_locked', 'user', actor_id=user.id, ip=ip, details={'scope': '2fa'})
            await db.commit()
            raise HTTPException(429, 'too many 2FA attempts')
        if not user.twofa_secret or not data.otp or not pyotp.TOTP(decrypt_secret(user.twofa_secret)).verify(data.otp, valid_window=1):
            locked = await record_auth_failure(user, '2fa', '2fa')
            await audit(db, '2fa_failed', 'user', actor_id=user.id, ip=ip)
            if locked:
                await audit(db, 'account_locked', 'user', actor_id=user.id, ip=ip, details={'scope': '2fa'})
            await db.commit()
            raise HTTPException(401, '2FA code required')
        await audit(db, '2fa_success', 'user', actor_id=user.id, ip=ip)
    elif user.role in STAFF_2FA_ROLES:
        await audit(db, 'login_2fa_setup_required', 'user', actor_id=user.id, ip=ip)
        await db.commit()
        raise HTTPException(403, '2FA setup required')
    await record_auth_success(user, 'login')
    auth_marker = auth_state_marker(user.password_hash)
    access = create_token(str(user.id), 'access', timedelta(minutes=settings.JWT_ACCESS_MINUTES), {'role':user.role, 'auth': auth_marker})
    refresh = create_token(str(user.id), 'refresh', timedelta(days=settings.JWT_REFRESH_DAYS), {'auth': auth_marker})
    refresh_payload = decode_token(refresh)
    db.add(RefreshToken(
        user_id=user.id,
        token_hash=sha256_text(refresh),
        jti=refresh_payload['jti'],
        expires_at=datetime.fromtimestamp(refresh_payload['exp'], tz=timezone.utc),
        ip=ip,
        user_agent=request.headers.get('user-agent', '')[:300],
    ))
    await audit(db, 'successful_login', 'user', actor_id=user.id, ip=ip)
    await db.commit()
    return TokenOut(access_token=access, refresh_token=refresh)

@router.post('/refresh', response_model=TokenOut)
async def refresh_token(data: RefreshIn, request: Request, db: AsyncSession=Depends(get_db)):
    retry_after = await hit_rate_limit('api-refresh', request, sha256_text(data.refresh_token)[:16], settings.REFRESH_RATE_LIMIT)
    if retry_after:
        raise HTTPException(429, 'too many refresh attempts')
    try:
        payload = decode_token(data.refresh_token)
        if payload.get('type') != 'refresh':
            raise ValueError()
    except Exception:
        raise HTTPException(401, 'invalid refresh token')
    stored = (await db.execute(select(RefreshToken).where(RefreshToken.token_hash == sha256_text(data.refresh_token)))).scalar_one_or_none()
    if not stored or stored.revoked_at or stored.expires_at <= datetime.now(timezone.utc):
        if stored:
            await audit(db, 'suspicious_refresh', 'user', actor_id=stored.user_id, ip=_client_ip(request), details={'reason': 'revoked_or_expired'})
            await db.commit()
        raise HTTPException(401, 'refresh token revoked or expired')
    user = (await db.execute(select(User).where(User.id == payload['sub']))).scalar_one_or_none()
    if not user or not user.is_active or user.is_locked:
        raise HTTPException(403, 'user blocked')
    auth_marker = auth_state_marker(user.password_hash)
    if payload.get('auth') != auth_marker:
        stored.revoked_at = datetime.now(timezone.utc)
        await db.commit()
        raise HTTPException(401, 'refresh token expired')
    stored.revoked_at = datetime.now(timezone.utc)
    access = create_token(str(user.id), 'access', timedelta(minutes=settings.JWT_ACCESS_MINUTES), {'role': user.role, 'auth': auth_marker})
    refresh = create_token(str(user.id), 'refresh', timedelta(days=settings.JWT_REFRESH_DAYS), {'auth': auth_marker})
    refresh_payload = decode_token(refresh)
    db.add(RefreshToken(
        user_id=user.id,
        token_hash=sha256_text(refresh),
        jti=refresh_payload['jti'],
        expires_at=datetime.fromtimestamp(refresh_payload['exp'], tz=timezone.utc),
        ip=_client_ip(request),
        user_agent=request.headers.get('user-agent', '')[:300],
    ))
    await audit(db, 'token_refreshed', 'user', actor_id=user.id, ip=_client_ip(request))
    await db.commit()
    return TokenOut(access_token=access, refresh_token=refresh)

@router.post('/logout')
async def logout(data: RefreshIn, request: Request, db: AsyncSession=Depends(get_db)):
    stored = (await db.execute(select(RefreshToken).where(RefreshToken.token_hash == sha256_text(data.refresh_token)))).scalar_one_or_none()
    if stored and not stored.revoked_at:
        stored.revoked_at = datetime.now(timezone.utc)
        await audit(db, 'token_revoked', 'user', actor_id=stored.user_id, ip=_client_ip(request))
        await audit(db, 'logout', 'user', actor_id=stored.user_id, ip=_client_ip(request))
        await db.commit()
    return {'ok': True}
