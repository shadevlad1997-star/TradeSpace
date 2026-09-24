"""Authenticated integration-management endpoints; no credentials are returned."""
from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from app.api.deps import current_user, require_roles
from app.core.enums import Role
from app.core.client_ip import client_ip
from app.db.session import get_db
from app.models import Merchant, User
from app.services.integration_modes import integration_status, change_production_access, IntegrationModeError

router = APIRouter(prefix='/integration', tags=['integration'])

class ProductionCommand(BaseModel):
    confirmation: str = Field(max_length=32)
    reason: str = Field(min_length=10, max_length=1000)

@router.get('/merchants/{merchant_id}')
async def status(merchant_id: UUID, actor: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    merchant = await db.get(Merchant, merchant_id)
    if not merchant or not (actor.role in {'admin','superadmin'} or (actor.role == 'merchant' and merchant.owner_id == actor.id)):
        raise HTTPException(404, 'merchant_not_found')
    if actor.role in {'admin','superadmin'} and not actor.twofa_enabled:
        raise HTTPException(403, '2FA setup required')
    return await integration_status(db, merchant)

@router.post('/merchants/{merchant_id}/production/{action}')
async def production(merchant_id: UUID, action: Literal['activate','suspend'], payload: ProductionCommand,
                     request: Request, actor: User = Depends(require_roles(Role.superadmin)), db: AsyncSession = Depends(get_db)):
    try:
        result = await change_production_access(db, merchant_id, actor, action=action,
            confirmation=payload.confirmation, reason=payload.reason, ip=client_ip(request))
    except IntegrationModeError as exc:
        raise HTTPException(exc.status, {'code': exc.code, 'message': str(exc), 'reasons': exc.reasons}) from exc
    await db.commit()
    return result


class AggregatorCredentialCommand(ProductionCommand):
    mode: Literal['sandbox','production']
    key_id: UUID | None = None


@router.get('/aggregators/{aggregator_id}')
async def aggregator_status(aggregator_id: UUID, actor: User = Depends(require_roles(Role.admin, Role.superadmin)), db: AsyncSession = Depends(get_db)):
    from app.models import AggregatorAccount
    from app.services.aggregator_credentials import aggregator_integration_status
    account = await db.get(AggregatorAccount, aggregator_id)
    if not account: raise HTTPException(404, 'aggregator_not_found')
    return await aggregator_integration_status(db, account)


@router.post('/aggregators/{aggregator_id}/production/{action}')
async def aggregator_production(aggregator_id: UUID, action: Literal['activate','suspend'], payload: ProductionCommand,
                               request: Request, actor: User = Depends(require_roles(Role.superadmin)), db: AsyncSession = Depends(get_db)):
    from app.services.aggregator_credentials import change_aggregator_access
    try:
        result = await change_aggregator_access(db, aggregator_id, actor, action=action,
            confirmation=payload.confirmation, reason=payload.reason, ip=client_ip(request))
    except IntegrationModeError as exc:
        raise HTTPException(exc.status, {'code':exc.code,'message':str(exc),'reasons':exc.reasons}) from exc
    await db.commit()
    return result


@router.post('/aggregators/{aggregator_id}/credentials/{action}')
async def aggregator_credentials(aggregator_id: UUID, action: Literal['issue','rotate','suspend','revoke'], payload: AggregatorCredentialCommand,
                                request: Request, actor: User = Depends(require_roles(Role.superadmin)), db: AsyncSession = Depends(get_db)):
    from fastapi.responses import JSONResponse
    from app.services.aggregator_credentials import change_aggregator_key
    try:
        key, secret = await change_aggregator_key(db, aggregator_id, actor, action=action,
            mode=payload.mode, key_id=payload.key_id, confirmation=payload.confirmation, reason=payload.reason, ip=client_ip(request))
    except IntegrationModeError as exc:
        raise HTTPException(exc.status, {'code':exc.code,'message':str(exc),'reasons':exc.reasons}) from exc
    await db.commit()
    result = {'id':str(key.id),'mode':key.mode,'status':key.status}
    if secret: result.update(api_key=key.api_key, secret_key=secret)
    return JSONResponse(result, headers={'Cache-Control':'no-store, max-age=0','Pragma':'no-cache','Referrer-Policy':'no-referrer'})
