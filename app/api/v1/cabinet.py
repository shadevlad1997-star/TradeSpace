from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_db
from app.api.deps import current_user, require_roles
from app.core.enums import DepositStatus, PayoutStatus, Role
from app.models import User, Merchant, Balance, Deposit, Payout, Requisite, Appeal, AppealMessage
from app.schemas.common import SmsIn, AppealIn
from app.services.sms import ingest_sms
router=APIRouter(prefix='/cabinet', tags=['cabinet'])

STAFF_ROLES = {Role.superadmin.value, Role.admin.value, Role.support.value}
TRADER_ROLES = {Role.operator.value, Role.trader.value}

async def my_merchant(db, user):
    m=(await db.execute(select(Merchant).where(Merchant.owner_id==user.id))).scalar_one_or_none()
    if not m: raise HTTPException(404,'merchant not found')
    return m

def _deposit_out(dep: Deposit) -> dict:
    return {
        'id': dep.id,
        'external_id': dep.external_id,
        'amount': dep.amount,
        'currency': dep.currency,
        'method': dep.method,
        'status': dep.status,
        'created_at': dep.created_at,
        'updated_at': dep.updated_at,
    }

def _payout_out(payout: Payout) -> dict:
    return {
        'id': payout.id,
        'external_id': payout.external_id,
        'amount': payout.amount,
        'currency': payout.currency,
        'method': payout.method,
        'status': payout.status,
        'created_at': payout.created_at,
        'updated_at': payout.updated_at,
    }

async def _operation_visible_to_user(db: AsyncSession, user: User, operation_type: str, operation_id):
    operation_type = operation_type.strip().lower()
    if operation_type not in {'deposit', 'payout'}:
        raise HTTPException(422, 'operation_type must be deposit or payout')

    model = Deposit if operation_type == 'deposit' else Payout
    operation = (await db.execute(select(model).where(model.id == operation_id))).scalar_one_or_none()
    if not operation:
        raise HTTPException(404, 'operation not found')

    if user.role in STAFF_ROLES:
        return operation

    if user.role == Role.merchant.value:
        merchant = await my_merchant(db, user)
        if operation.merchant_id != merchant.id:
            raise HTTPException(404, 'operation not found')
        return operation

    if user.role in TRADER_ROLES and operation_type == 'deposit':
        if not operation.requisites_id:
            raise HTTPException(404, 'operation not found')
        req = (await db.execute(select(Requisite).where(Requisite.id == operation.requisites_id))).scalar_one_or_none()
        if not req or req.trader_id != user.id:
            raise HTTPException(404, 'operation not found')
        return operation

    raise HTTPException(403, 'not enough permissions')

@router.get('/wallet')
async def wallet(db: AsyncSession=Depends(get_db), user: User=Depends(current_user)):
    m=await my_merchant(db,user)
    b=(await db.execute(select(Balance).where(Balance.merchant_id==m.id))).scalar_one_or_none()
    return {'merchant':m.name,'available':b.available if b else 0,'frozen':b.frozen if b else 0,'holds':b.frozen if b else 0,'history_endpoint':'/api/v1/cabinet/operations'}

@router.get('/operations')
async def operations(db: AsyncSession=Depends(get_db), user: User=Depends(current_user)):
    m=await my_merchant(db,user)
    deps=(await db.execute(select(Deposit).where(Deposit.merchant_id==m.id).order_by(Deposit.created_at.desc()).limit(100))).scalars().all()
    pays=(await db.execute(select(Payout).where(Payout.merchant_id==m.id).order_by(Payout.created_at.desc()).limit(100))).scalars().all()
    return {'deposits':[_deposit_out(dep) for dep in deps],'payouts':[_payout_out(payout) for payout in pays]}

@router.get('/statistics')
async def statistics(db: AsyncSession=Depends(get_db), user: User=Depends(current_user)):
    m=await my_merchant(db,user)
    turnover=(await db.execute(select(func.coalesce(func.sum(Deposit.amount),0)).where(Deposit.merchant_id==m.id, Deposit.status==DepositStatus.paid.value))).scalar_one()
    successful_deposits=(await db.execute(select(func.count()).select_from(Deposit).where(Deposit.merchant_id==m.id, Deposit.status==DepositStatus.paid.value))).scalar_one()
    failed_deposits=(await db.execute(select(func.count()).select_from(Deposit).where(Deposit.merchant_id==m.id, Deposit.status.in_([DepositStatus.failed.value,DepositStatus.cancelled.value,DepositStatus.expired.value])))).scalar_one()
    successful_payouts=(await db.execute(select(func.count()).select_from(Payout).where(Payout.merchant_id==m.id, Payout.status==PayoutStatus.completed.value))).scalar_one()
    failed_payouts=(await db.execute(select(func.count()).select_from(Payout).where(Payout.merchant_id==m.id, Payout.status.in_([PayoutStatus.failed.value,PayoutStatus.cancelled.value,PayoutStatus.rejected.value])))).scalar_one()
    return {'turnover':turnover,'successful_deposits':successful_deposits,'failed_deposits':failed_deposits,'successful_payouts':successful_payouts,'failed_payouts':failed_payouts}

@router.post('/sms')
async def sms(data: SmsIn, db: AsyncSession=Depends(get_db), user: User=Depends(require_roles(Role.superadmin,Role.admin,Role.support,Role.operator,Role.trader))):
    s=await ingest_sms(db,data.provider_message_id,data.sender,data.body); await db.commit(); await db.refresh(s); return s

@router.post('/appeals')
async def create_appeal(data: AppealIn, db: AsyncSession=Depends(get_db), user: User=Depends(current_user)):
    operation_type = data.operation_type.strip().lower()
    operation = await _operation_visible_to_user(db, user, operation_type, data.operation_id)
    final_statuses = {
        DepositStatus.paid.value,
        DepositStatus.failed.value,
        DepositStatus.cancelled.value,
        DepositStatus.expired.value,
        PayoutStatus.completed.value,
        PayoutStatus.failed.value,
        PayoutStatus.cancelled.value,
    }
    previous_status = operation.status
    if getattr(operation, 'status', None) not in final_statuses:
        operation.status = DepositStatus.appeal_opened.value if operation_type == 'deposit' else PayoutStatus.appeal_opened.value
    a=Appeal(operation_type=operation_type, operation_id=data.operation_id, created_by=user.id, metadata_json={f'previous_{operation_type}_status': previous_status})
    db.add(a); await db.flush(); db.add(AppealMessage(appeal_id=a.id, author_id=user.id, message=data.message)); await db.commit(); await db.refresh(a); return a
