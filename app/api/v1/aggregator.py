import time
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import hmac_replay_hashes
from app.core.client_ip import client_ip
from app.core.config import settings
from app.core.security import decrypt_secret, verify_hmac
from app.db.session import get_db
from app.models import AggregatorAccount, AggregatorApiKey, AggregatorPayment, AggregatorReplayNonce, ApiRequestLog, Deposit
from app.schemas.common import AggregatorPaymentCreate
from app.services.aggregator_credentials import authorize_aggregator_key
from app.services.integration_modes import IntegrationModeError
from app.services.aggregator_enqueue import enqueue_aggregator_callback_delivery
from app.services.aggregators import AggregatorError, create_payment, payment_response, sync_payment_from_deposit

router = APIRouter(prefix='/aggregator/v1', tags=['aggregator-api'])


async def aggregator_auth(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_api_key: str = Header(..., alias='X-API-Key'),
    x_timestamp: str = Header(..., alias='X-Timestamp'),
    x_signature: str = Header(..., alias='X-Signature'),
) -> AggregatorAccount:
    key = await db.scalar(select(AggregatorApiKey).where(AggregatorApiKey.api_key == x_api_key))
    if not key:
        raise HTTPException(401, 'bad api key')
    account = await db.get(AggregatorAccount, key.aggregator_id)
    if not account:
        raise HTTPException(401, 'bad api key')
    if account.status != 'active' or getattr(account, 'is_archived', False):
        raise HTTPException(403, 'aggregator blocked')
    try:
        timestamp = int(x_timestamp)
    except ValueError:
        raise HTTPException(401, 'bad timestamp')
    if abs(int(time.time()) - timestamp) > settings.HMAC_TIMESTAMP_TOLERANCE_SECONDS:
        raise HTTPException(401, 'stale timestamp')
    body = await request.body()
    secret = decrypt_secret(key.encrypted_secret)
    if not secret:
        raise HTTPException(401, 'aggregator api secret needs regeneration')
    if not verify_hmac(secret, x_timestamp, body, x_signature):
        raise HTTPException(401, 'bad signature')

    try:
        await authorize_aggregator_key(db, account, key)
    except IntegrationModeError as exc:
        raise HTTPException(exc.status, {'code':exc.code,'message':str(exc)}) from exc
    request_hashes = hmac_replay_hashes(request, body)
    now = datetime.now(timezone.utc)
    existing_replay = (await db.execute(
        select(AggregatorReplayNonce).where(
            AggregatorReplayNonce.aggregator_id == account.id,
            AggregatorReplayNonce.request_hash.in_(request_hashes),
            AggregatorReplayNonce.expires_at > now,
        ).limit(1)
    )).scalar_one_or_none()
    if existing_replay:
        raise HTTPException(409, 'hmac replay detected')
    expires_at = now + timedelta(seconds=settings.HMAC_TIMESTAMP_TOLERANCE_SECONDS)
    for request_hash in request_hashes:
        db.add(AggregatorReplayNonce(
            aggregator_id=account.id,
            signature=x_signature,
            request_hash=request_hash,
            timestamp=timestamp,
            expires_at=expires_at,
        ))
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, 'hmac replay detected') from exc
    key.last_used_at = now
    return account


async def log_aggregator_request(db: AsyncSession, account: AggregatorAccount, request: Request, status: int = 200) -> None:
    db.add(ApiRequestLog(
        merchant_id=account.platform_merchant_id,
        method=request.method,
        path=request.url.path,
        ip=client_ip(request),
        status_code=status,
        request_id=request.headers.get('X-Request-ID', ''),
    ))


@router.post('/payments')
async def create_aggregator_payment(
    data: AggregatorPaymentCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    account: AggregatorAccount = Depends(aggregator_auth),
    x_request_id: str = Header(..., alias='X-Request-ID'),
    x_idempotency_key: str = Header(..., alias='X-Idempotency-Key'),
):
    idem = x_idempotency_key.strip()[:128]
    if not idem:
        raise HTTPException(422, 'X-Idempotency-Key is required')
    try:
        result = await create_payment(
            db,
            aggregator=account,
            aggregator_order_id=data.aggregator_order_id,
            merchant_order_id=data.merchant_order_id,
            external_merchant_id=data.external_merchant_id,
            amount=data.amount,
            currency=data.currency,
            payment_method=data.payment_method,
            client_id=data.client_id,
            client_ip=data.client_ip or client_ip(request),
            callback_url=data.callback_url,
            success_url=data.success_url,
            fail_url=data.fail_url,
            idempotency_key=idem,
        )
    except AggregatorError as exc:
        status_code = 503 if str(exc) == 'rolling_rate_unavailable' else 409
        await log_aggregator_request(db, account, request, status_code)
        await db.commit()
        raise HTTPException(status_code, str(exc))
    except ValueError as exc:
        await log_aggregator_request(db, account, request, 409)
        await db.commit()
        raise HTTPException(409, str(exc))
    await log_aggregator_request(db, account, request, 200)
    await db.commit()
    await db.refresh(result.payment)
    await db.refresh(result.deposit)
    return await payment_response(db, result)


@router.get('/payments/{platform_payment_id}')
async def get_aggregator_payment(
    platform_payment_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    account: AggregatorAccount = Depends(aggregator_auth),
):
    payment = (await db.execute(
        select(AggregatorPayment).where(
            AggregatorPayment.aggregator_id == account.id,
            AggregatorPayment.platform_payment_id == platform_payment_id,
        )
    )).scalar_one_or_none()
    if not payment:
        await log_aggregator_request(db, account, request, 404)
        await db.commit()
        raise HTTPException(404, 'payment not found')
    dep = (await db.execute(select(Deposit).where(Deposit.id == payment.platform_payment_id))).scalar_one_or_none()
    if dep:
        await sync_payment_from_deposit(db, dep)
    await log_aggregator_request(db, account, request, 200)
    await db.commit()
    return {
        'platform_payment_id': str(payment.platform_payment_id),
        'aggregator_order_id': payment.aggregator_order_id,
        'merchant_order_id': payment.merchant_order_id,
        'status': payment.status,
        'amount': str(payment.amount),
        'currency': payment.currency,
        'created_at': payment.created_at.isoformat(),
        'expires_at': payment.expires_at.isoformat(),
        'paid_at': payment.paid_at.isoformat() if payment.paid_at else None,
    }


@router.post('/payments/{platform_payment_id}/callbacks/retry')
async def retry_aggregator_payment_callbacks(
    platform_payment_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    account: AggregatorAccount = Depends(aggregator_auth),
):
    payment = (await db.execute(
        select(AggregatorPayment).where(
            AggregatorPayment.aggregator_id == account.id,
            AggregatorPayment.platform_payment_id == platform_payment_id,
        )
    )).scalar_one_or_none()
    if not payment:
        await log_aggregator_request(db, account, request, 404)
        await db.commit()
        raise HTTPException(404, 'payment not found')
    dep = (await db.execute(select(Deposit).where(Deposit.id == payment.platform_payment_id))).scalar_one_or_none()
    logs = await sync_payment_from_deposit(db, dep) if dep else []
    await log_aggregator_request(db, account, request, 200)
    await db.commit()
    enqueued = [enqueue_aggregator_callback_delivery(item.id) for item in logs]
    return {'ok': True, 'callbacks_created': len(logs), 'callbacks_enqueued': sum(1 for item in enqueued if item)}
