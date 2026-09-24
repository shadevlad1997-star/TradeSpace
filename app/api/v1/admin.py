import csv
import io
import secrets
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

import pyotp
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_db
from app.api.deps import require_roles
from app.core.business_rules import BUSINESS_RULES, ROLE_PERMISSIONS
from app.core.compliance import COMPLIANCE_POLICY
from app.models import Appeal, AppealMessage, AggregatorAccount, AggregatorCallbackLog, AggregatorPayment, Blacklist, FeeRule, KycProfile, LedgerEntry, LimitRule, ReportExport, RiskEvent, User, Merchant, ApiKey, Deposit, Payout, Balance, Requisite, AuditLog, WebhookEvent, MerchantWebhookSigningKey
from app.schemas.common import AggregatorCreate, AggregatorStatusIn, AggregatorUpdate, AppealMessageIn, AppealResolveIn, BlacklistIn, FeeRuleIn, KycReviewIn, LimitRuleIn, MerchantCreate, RequisiteIn, UserCreateIn
from app.core.enums import AppealStatus, Role, DepositStatus, PayoutStatus
from app.core.access import can_message_appeal, can_view_admin_stats
from app.core.requisite_providers import requisite_provider_payload
from app.core.security import encrypt_secret, hash_password, reveal_text
from app.core.client_ip import client_ip
from app.services.compliance import get_compliance_summary, list_kyc_profiles, merchant_compliance_snapshot, review_kyc_profile
from app.services.aggregator_enqueue import enqueue_aggregator_callback_delivery
from app.services.aggregators import create_aggregator_account, money, percent, regenerate_aggregator_secret, sync_payment_from_deposit
from app.services.antiscam import check_payment_result_for_risk
from app.services.payouts import complete_payout as complete_payout_flow, release_payout_hold
from app.services.deposit_confirmation import DepositConfirmationError, confirm_deposit_payment, enqueue_deposit_confirmation_deliveries
from app.services.audit import audit
from app.services.appeals import resolve_operation_appeal
from app.services.fee_tiers import (
    FeeConfigurationError,
    create_fee_rule as create_tier_fee_rule,
    replace_fee_rule,
)
from app.services.platform_income import platform_income_dashboard
from app.services.webhooks import (
    queue_webhook,
    request_manual_webhook_retry,
)
from app.services.webhook_signing_keys import (
    WebhookSigningKeyError,
    issue_webhook_signing_key,
    revoke_webhook_signing_key,
    rotate_webhook_signing_key,
    webhook_key_fingerprint,
)
from app.services.sms import confirm_deposit_from_sms
from app.services.webhook_enqueue import enqueue_webhook_delivery
from app.services.webhook_payloads import build_deposit_webhook_payload, build_payout_webhook_payload
router=APIRouter(prefix='/admin', tags=['admin'])

def _enqueue_webhook(event_id) -> bool:
    return enqueue_webhook_delivery(event_id)

def _kyc_public(profile: KycProfile) -> dict:
    return {
        'id': profile.id,
        'merchant_id': profile.merchant_id,
        'status': profile.status,
        'legal_name': profile.legal_name,
        'country': profile.country,
        'risk_level': profile.risk_level,
        'reviewed_by': profile.reviewed_by,
        'reviewed_at': profile.reviewed_at,
        'created_at': profile.created_at,
        'updated_at': profile.updated_at,
    }

def _risk_public(event: RiskEvent) -> dict:
    return {
        'id': event.id,
        'merchant_id': event.merchant_id,
        'operation_type': event.operation_type,
        'operation_id': event.operation_id,
        'score': event.score,
        'decision': event.decision,
        'reason': event.reason,
        'details': event.details,
        'created_at': event.created_at,
    }


def _aggregator_public(account: AggregatorAccount) -> dict:
    return {
        'id': account.id,
        'platform_merchant_id': account.platform_merchant_id,
        'name': account.name,
        'status': account.status,
        'api_key': account.api_key,
        'callback_url': account.callback_url,
        'success_url': account.success_url,
        'fail_url': account.fail_url,
        'commission_percent': account.commission_percent,
        'min_payment_amount': account.min_payment_amount,
        'max_payment_amount': account.max_payment_amount,
        'daily_limit': account.daily_limit,
        'monthly_limit': account.monthly_limit,
        'balance': account.balance,
        'hold_balance': account.hold_balance,
        'total_turnover': account.total_turnover,
        'created_at': account.created_at,
        'updated_at': account.updated_at,
    }


def _aggregator_payment_public(payment: AggregatorPayment) -> dict:
    return {
        'id': payment.id,
        'aggregator_id': payment.aggregator_id,
        'aggregator_merchant_id': payment.aggregator_merchant_id,
        'merchant_order_id': payment.merchant_order_id,
        'aggregator_order_id': payment.aggregator_order_id,
        'platform_payment_id': payment.platform_payment_id,
        'amount': payment.amount,
        'currency': payment.currency,
        'payment_method': payment.payment_method,
        'status': payment.status,
        'callback_status': payment.callback_status,
        'callback_attempts': payment.callback_attempts,
        'expires_at': payment.expires_at,
        'paid_at': payment.paid_at,
        'created_at': payment.created_at,
    }


def _aggregator_callback_public(log: AggregatorCallbackLog) -> dict:
    return {
        'id': log.id,
        'direction': log.direction,
        'related_payment_id': log.related_payment_id,
        'target_url': log.target_url,
        'status': log.status,
        'attempt': log.attempt,
        'response_status_code': log.response_status_code,
        'error_message': log.error_message,
        'next_retry_at': log.next_retry_at,
        'sent_at': log.sent_at,
        'created_at': log.created_at,
    }


def _apply_aggregator_update(account: AggregatorAccount, data: AggregatorUpdate) -> None:
    fields = data.model_fields_set
    if 'name' in fields and data.name is not None:
        account.name = data.name
    if 'callback_url' in fields:
        account.callback_url = data.callback_url
    if 'success_url' in fields:
        account.success_url = data.success_url
    if 'fail_url' in fields:
        account.fail_url = data.fail_url
    if 'commission_percent' in fields and data.commission_percent is not None:
        account.commission_percent = percent(data.commission_percent)
    if 'min_payment_amount' in fields and data.min_payment_amount is not None:
        account.min_payment_amount = money(data.min_payment_amount)
    if 'max_payment_amount' in fields and data.max_payment_amount is not None:
        account.max_payment_amount = money(data.max_payment_amount)
    if 'daily_limit' in fields:
        account.daily_limit = money(data.daily_limit) if data.daily_limit is not None else None
    if 'monthly_limit' in fields:
        account.monthly_limit = money(data.monthly_limit) if data.monthly_limit is not None else None
    if money(account.max_payment_amount) < money(account.min_payment_amount):
        raise ValueError('max_payment_amount cannot be lower than min_payment_amount')


def _requisite_public(req: Requisite, *, include_value: bool) -> dict:
    provider = requisite_provider_payload(req.method, req.bank_code, req.operator_code, req.bank_name)
    return {
        'id': req.id,
        'trader_id': req.trader_id,
        'owner_name': req.owner_name,
        'full_name': req.full_name,
        'method': req.method,
        'value': reveal_text(req.value_encrypted) if include_value else '',
        'bank_code': req.bank_code,
        'operator_code': req.operator_code,
        'bank_name': req.bank_name,
        **provider,
        'automation_id': req.automation_id,
        'last4': req.last4,
        'min_check': req.min_check,
        'max_check': req.max_check,
        'daily_limit': req.daily_limit,
        'operation_limit': req.operation_limit,
        'request_count': req.request_count,
        'timeframe': req.timeframe,
        'success_delay_minutes': req.success_delay_minutes,
        'simultaneous_limit': req.simultaneous_limit,
        'enabled': req.enabled,
        'status': req.status,
        'usage_count': req.usage_count,
        'last_success_at': req.last_success_at,
        'created_at': req.created_at,
        'updated_at': req.updated_at,
    }

@router.post('/merchants')
async def create_merchant(data: MerchantCreate, request: Request, db: AsyncSession=Depends(get_db), actor=Depends(require_roles(Role.superadmin, Role.admin))):
    if (await db.execute(select(User).where(User.email==data.email))).scalar_one_or_none(): raise HTTPException(409,'email exists')
    user=User(email=data.email, password_hash=hash_password(data.password), role=Role.merchant.value)
    db.add(user); await db.flush()
    merchant=Merchant(owner_id=user.id, name=data.name, webhook_url=data.webhook_url, ip_whitelist=data.ip_whitelist, sandbox_mode=data.sandbox_mode)
    db.add(merchant); await db.flush()
    secret=secrets.token_urlsafe(32); api_key='pk_'+secrets.token_urlsafe(24)
    db.add(ApiKey(merchant_id=merchant.id, api_key=api_key, secret_hash=encrypt_secret(secret), mode='sandbox' if data.sandbox_mode else 'production'))
    db.add(Balance(merchant_id=merchant.id, available=Decimal('0.00')))
    db.add(KycProfile(merchant_id=merchant.id, status='not_started', risk_level='standard'))
    await audit(db,'merchant_created','merchant',actor.id,merchant.id,client_ip(request))
    await db.commit()
    return {'merchant_id':merchant.id,'api_key':api_key,'secret_key':secret,'warning':'Secret is shown once. Store it safely.'}


@router.get('/aggregators')
async def list_aggregators(db: AsyncSession=Depends(get_db), actor=Depends(require_roles(Role.superadmin, Role.admin, Role.support))):
    rows = (await db.execute(select(AggregatorAccount).order_by(AggregatorAccount.created_at.desc()).limit(300))).scalars().all()
    return [_aggregator_public(row) for row in rows]


@router.post('/aggregators')
async def create_aggregator(data: AggregatorCreate, request: Request, db: AsyncSession=Depends(get_db), actor=Depends(require_roles(Role.superadmin, Role.admin))):
    try:
        account, secret = await create_aggregator_account(
            db,
            name=data.name,
            callback_url=data.callback_url,
            success_url=data.success_url,
            fail_url=data.fail_url,
            commission_percent=data.commission_percent,
            min_payment_amount=data.min_payment_amount,
            max_payment_amount=data.max_payment_amount,
            daily_limit=data.daily_limit,
            monthly_limit=data.monthly_limit,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    await audit(db, 'aggregator_created', 'aggregator', actor.id, account.id, client_ip(request), {'name': account.name})
    await db.commit()
    data_out = _aggregator_public(account)
    data_out.update({'secret_key': secret, 'warning': 'Secret is shown once. Store it safely.'})
    return data_out


@router.patch('/aggregators/{aggregator_id}')
async def update_aggregator(aggregator_id: UUID, data: AggregatorUpdate, request: Request, db: AsyncSession=Depends(get_db), actor=Depends(require_roles(Role.superadmin, Role.admin))):
    account = (await db.execute(select(AggregatorAccount).where(AggregatorAccount.id == aggregator_id).with_for_update())).scalar_one_or_none()
    if not account:
        raise HTTPException(404, 'aggregator not found')
    try:
        _apply_aggregator_update(account, data)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    await audit(db, 'aggregator_updated', 'aggregator', actor.id, account.id, client_ip(request), {'fields': sorted(data.model_fields_set)})
    await db.commit()
    await db.refresh(account)
    return _aggregator_public(account)


@router.post('/aggregators/{aggregator_id}/status')
async def set_aggregator_status(aggregator_id: UUID, data: AggregatorStatusIn, request: Request, db: AsyncSession=Depends(get_db), actor=Depends(require_roles(Role.superadmin, Role.admin))):
    account = (await db.execute(select(AggregatorAccount).where(AggregatorAccount.id == aggregator_id).with_for_update())).scalar_one_or_none()
    if not account:
        raise HTTPException(404, 'aggregator not found')
    account.status = data.status
    await audit(db, 'aggregator_status_changed', 'aggregator', actor.id, account.id, client_ip(request), {'status': account.status})
    await db.commit()
    await db.refresh(account)
    return _aggregator_public(account)


@router.post('/aggregators/{aggregator_id}/secret/regenerate')
async def regenerate_aggregator_api_secret(aggregator_id: UUID, request: Request, db: AsyncSession=Depends(get_db), actor=Depends(require_roles(Role.superadmin))):
    account = (await db.execute(select(AggregatorAccount).where(AggregatorAccount.id == aggregator_id).with_for_update())).scalar_one_or_none()
    if not account:
        raise HTTPException(404, 'aggregator not found')
    secret = await regenerate_aggregator_secret(db, account)
    await audit(db, 'aggregator_secret_regenerated', 'aggregator', actor.id, account.id, client_ip(request))
    await db.commit()
    data_out = _aggregator_public(account)
    data_out.update({'secret_key': secret, 'warning': 'Secret is shown once. Store it safely.'})
    return data_out


@router.get('/aggregators/{aggregator_id}/payments')
async def list_aggregator_payments(aggregator_id: UUID, db: AsyncSession=Depends(get_db), actor=Depends(require_roles(Role.superadmin, Role.admin, Role.support))):
    account = (await db.execute(select(AggregatorAccount).where(AggregatorAccount.id == aggregator_id))).scalar_one_or_none()
    if not account:
        raise HTTPException(404, 'aggregator not found')
    rows = (await db.execute(
        select(AggregatorPayment)
        .where(AggregatorPayment.aggregator_id == aggregator_id)
        .order_by(AggregatorPayment.created_at.desc())
        .limit(300)
    )).scalars().all()
    return [_aggregator_payment_public(row) for row in rows]


@router.get('/aggregators/{aggregator_id}/callbacks')
async def list_aggregator_callbacks(aggregator_id: UUID, db: AsyncSession=Depends(get_db), actor=Depends(require_roles(Role.superadmin, Role.admin, Role.support))):
    account = (await db.execute(select(AggregatorAccount).where(AggregatorAccount.id == aggregator_id))).scalar_one_or_none()
    if not account:
        raise HTTPException(404, 'aggregator not found')
    payment_ids = select(AggregatorPayment.id).where(AggregatorPayment.aggregator_id == aggregator_id)
    rows = (await db.execute(
        select(AggregatorCallbackLog)
        .where(AggregatorCallbackLog.related_payment_id.in_(payment_ids))
        .order_by(AggregatorCallbackLog.created_at.desc())
        .limit(300)
    )).scalars().all()
    return [_aggregator_callback_public(row) for row in rows]

@router.post('/users')
async def create_user(data: UserCreateIn, request: Request, db: AsyncSession=Depends(get_db), actor=Depends(require_roles(Role.superadmin, Role.admin))):
    if actor.role == Role.admin.value and data.role == Role.admin:
        raise HTTPException(403, 'admin cannot create another admin')
    if data.role == Role.superadmin:
        raise HTTPException(403, 'superadmin is created only by seed')
    if (await db.execute(select(User).where(User.email==data.email))).scalar_one_or_none():
        raise HTTPException(409, 'email exists')
    needs_2fa = data.role in [Role.admin, Role.support, Role.teamlead]
    user=User(email=data.email, password_hash=hash_password(data.password), role=data.role.value, twofa_enabled=False, twofa_secret=encrypt_secret(pyotp.random_base32()) if needs_2fa else None)
    db.add(user); await db.flush()
    result={'user_id':user.id,'email':user.email,'role':user.role,'twofa_setup_required':needs_2fa}
    if data.role == Role.merchant:
        merchant=Merchant(owner_id=user.id, name=data.merchant_name or data.email, webhook_url=None, ip_whitelist=[], sandbox_mode=True)
        db.add(merchant); await db.flush()
        secret=secrets.token_urlsafe(32); api_key='pk_'+secrets.token_urlsafe(24)
        db.add(ApiKey(merchant_id=merchant.id, api_key=api_key, secret_hash=encrypt_secret(secret), mode='sandbox'))
        db.add(Balance(merchant_id=merchant.id, available=Decimal('0.00')))
        db.add(KycProfile(merchant_id=merchant.id, status='not_started', risk_level='standard'))
        result.update({'merchant_id':merchant.id,'api_key':api_key,'secret_key':secret})
    await audit(db,'user_created','user',actor.id,user.id,client_ip(request),{'role':user.role})
    await db.commit()
    return result

@router.post('/requisites')
async def add_requisite(data: RequisiteIn, request: Request, db: AsyncSession=Depends(get_db), actor=Depends(require_roles(Role.superadmin,Role.admin,Role.operator,Role.trader))):
    if actor.role in [Role.operator.value, Role.trader.value]:
        trader_id = actor.id
    else:
        if not data.trader_id:
            raise HTTPException(422, 'trader_id is required when admin creates a requisite')
        trader = (await db.execute(
            select(User).where(
                User.id == data.trader_id,
                User.role.in_([Role.operator.value, Role.trader.value]),
                User.is_active == True,
                User.is_locked == False,
            )
        )).scalar_one_or_none()
        if not trader:
            raise HTTPException(404, 'active trader not found')
        trader_id = trader.id
    req=Requisite(
        trader_id=trader_id,
        owner_name=data.owner_name,
        full_name=data.full_name or data.owner_name,
        method=data.method.value,
        value_encrypted=encrypt_secret(data.value.strip()),
        bank_code=data.bank_code,
        operator_code=data.operator_code,
        bank_name=data.bank_name,
        automation_id=data.automation_id,
        last4=data.last4,
        min_check=data.min_check,
        max_check=data.max_check,
        request_count=data.request_count,
        timeframe=data.timeframe,
        success_delay_minutes=data.success_delay_minutes,
        simultaneous_limit=data.simultaneous_limit,
        daily_limit=data.daily_limit,
        operation_limit=data.operation_limit,
    )
    db.add(req); await audit(db,'requisite_created','requisite',actor.id,None,client_ip(request),{'trader_id':str(trader_id),'method':data.method.value}); await db.commit(); await db.refresh(req)
    return _requisite_public(req, include_value=True)


@router.get('/requisites')
async def list_requisites(request: Request, db: AsyncSession=Depends(get_db), actor=Depends(require_roles(Role.superadmin,Role.admin,Role.support,Role.merchant,Role.operator,Role.trader))):
    if actor.role in [Role.superadmin.value, Role.admin.value, Role.support.value]:
        rows = (await db.execute(select(Requisite).order_by(Requisite.created_at.desc()).limit(500))).scalars().all()
        payload = [_requisite_public(req, include_value=True) for req in rows]
    elif actor.role in [Role.operator.value, Role.trader.value]:
        rows = (await db.execute(select(Requisite).where(Requisite.trader_id == actor.id).order_by(Requisite.created_at.desc()).limit(500))).scalars().all()
        payload = [_requisite_public(req, include_value=True) for req in rows]
    else:
        merchant = (await db.execute(select(Merchant).where(Merchant.owner_id == actor.id))).scalar_one_or_none()
        if not merchant:
            payload = []
        else:
            req_ids = (await db.execute(
                select(Deposit.requisites_id)
                .where(Deposit.merchant_id == merchant.id, Deposit.requisites_id.is_not(None))
                .distinct()
            )).scalars().all()
            if not req_ids:
                payload = []
            else:
                rows = (await db.execute(select(Requisite).where(Requisite.id.in_(req_ids)).order_by(Requisite.created_at.desc()))).scalars().all()
                payload = [_requisite_public(req, include_value=True) for req in rows]
    await audit(db, 'requisites_viewed', 'requisite', actor.id, None, client_ip(request), {'count': len(payload), 'role': actor.role})
    await db.commit()
    return payload

@router.get('/business-rules')
async def business_rules(db: AsyncSession=Depends(get_db), actor=Depends(require_roles(Role.superadmin, Role.admin, Role.support))):
    fees=(await db.execute(select(FeeRule).order_by(FeeRule.created_at.desc()).limit(200))).scalars().all()
    limits=(await db.execute(select(LimitRule).order_by(LimitRule.created_at.desc()).limit(200))).scalars().all()
    return {
        'role_permissions': ROLE_PERMISSIONS,
        'business_rules': BUSINESS_RULES,
        'fee_rules': fees,
        'limit_rules': limits,
    }

@router.get('/fee-rules')
async def list_fee_rules(db: AsyncSession=Depends(get_db), actor=Depends(require_roles(Role.superadmin, Role.admin))):
    return (await db.execute(select(FeeRule).order_by(FeeRule.created_at.desc()).limit(300))).scalars().all()


@router.get('/platform-income')
async def get_platform_income(
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    merchant_id: UUID | None = Query(default=None),
    trader_id: UUID | None = Query(default=None),
    aggregator_id: UUID | None = Query(default=None),
    payment_method: str | None = Query(default=None),
    currency: str | None = Query(default=None, max_length=8),
    executor_type: str | None = Query(default=None, pattern='^(trader|aggregator)$'),
    operation_status: str | None = Query(default=None, max_length=32),
    settlement_status: str | None = Query(default=None),
    amount_min: Decimal | None = Query(default=None, ge=0),
    amount_max: Decimal | None = Query(default=None, ge=0),
    margin: str | None = Query(default=None, pattern='^(positive|zero|negative)$'),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_roles(Role.superadmin, Role.admin)),
):
    return await platform_income_dashboard(
        db,
        date_from=date_from,
        date_to=date_to,
        merchant_id=merchant_id,
        trader_id=trader_id,
        aggregator_id=aggregator_id,
        payment_method=payment_method,
        currency=currency,
        executor_type=executor_type,
        operation_status=operation_status,
        settlement_status=settlement_status,
        amount_min=amount_min,
        amount_max=amount_max,
        margin=margin,
        page=page,
        page_size=page_size,
    )

@router.post('/fee-rules')
async def create_fee_rule(data: FeeRuleIn, request: Request, db: AsyncSession=Depends(get_db), actor=Depends(require_roles(Role.superadmin, Role.admin))):
    try:
        rule = await create_tier_fee_rule(
            db,
            actor_id=actor.id,
            entity_type=data.entity_type,
            entity_id=data.entity_id,
            fee_side=data.fee_side,
            payment_method=data.payment_method.value,
            currency=data.currency,
            min_amount=data.min_amount,
            max_amount=data.max_amount,
            rate_percent=data.rate_percent,
            effective_from=data.effective_from,
            effective_to=data.effective_to,
        )
    except FeeConfigurationError as exc:
        await db.rollback()
        raise HTTPException(422, {'code': getattr(exc, 'code', 'fee_configuration_error')}) from exc
    await audit(db,'fee_rule_created','fee_rule',actor.id,rule.id,client_ip(request),{'entity_type':data.entity_type,'entity_id':str(data.entity_id),'fee_side':data.fee_side,'payment_method':data.payment_method.value,'currency':data.currency,'min_amount':str(data.min_amount),'max_amount':str(data.max_amount) if data.max_amount is not None else None,'rate_percent':str(data.rate_percent)})
    await db.commit(); await db.refresh(rule)
    return rule


@router.post('/fee-rules/{rule_id}/replace')
async def replace_fee_rule_version(rule_id: UUID, data: FeeRuleIn, request: Request, db: AsyncSession=Depends(get_db), actor=Depends(require_roles(Role.superadmin, Role.admin))):
    current = (await db.execute(select(FeeRule).where(FeeRule.id == rule_id).with_for_update())).scalar_one_or_none()
    if not current:
        raise HTTPException(404, 'fee rule not found')
    try:
        replacement = await replace_fee_rule(
            db,
            current,
            actor_id=actor.id,
            entity_type=data.entity_type,
            entity_id=data.entity_id,
            fee_side=data.fee_side,
            payment_method=data.payment_method.value,
            currency=data.currency,
            min_amount=data.min_amount,
            max_amount=data.max_amount,
            rate_percent=data.rate_percent,
            effective_from=data.effective_from,
            effective_to=data.effective_to,
        )
    except FeeConfigurationError as exc:
        await db.rollback()
        raise HTTPException(422, {'code': getattr(exc, 'code', 'fee_configuration_error')}) from exc
    await audit(db, 'fee_rule_replaced', 'fee_rule', actor.id, replacement.id, client_ip(request), {'previous_rule_id': str(current.id), 'version': replacement.version})
    await db.commit()
    await db.refresh(replacement)
    return replacement


@router.post('/fee-rules/{rule_id}/deactivate')
async def deactivate_fee_rule(rule_id: UUID, request: Request, db: AsyncSession=Depends(get_db), actor=Depends(require_roles(Role.superadmin, Role.admin))):
    rule = (await db.execute(select(FeeRule).where(FeeRule.id == rule_id).with_for_update())).scalar_one_or_none()
    if not rule:
        raise HTTPException(404, 'fee rule not found')
    rule.is_active = False
    rule.effective_to = min(rule.effective_to, datetime.now(timezone.utc)) if rule.effective_to else datetime.now(timezone.utc)
    rule.updated_by = actor.id
    await audit(db, 'fee_rule_deactivated', 'fee_rule', actor.id, rule.id, client_ip(request))
    await db.commit()
    return {'id': rule.id, 'is_active': rule.is_active}

@router.get('/limit-rules')
async def list_limit_rules(db: AsyncSession=Depends(get_db), actor=Depends(require_roles(Role.superadmin, Role.admin, Role.support))):
    return (await db.execute(select(LimitRule).order_by(LimitRule.created_at.desc()).limit(300))).scalars().all()

@router.post('/limit-rules')
async def create_limit_rule(data: LimitRuleIn, request: Request, db: AsyncSession=Depends(get_db), actor=Depends(require_roles(Role.superadmin, Role.admin))):
    if data.min_amount > data.max_amount:
        raise HTTPException(422,'min_amount cannot be greater than max_amount')
    rule=LimitRule(merchant_id=data.merchant_id, method=data.method.value if data.method else None, min_amount=data.min_amount, max_amount=data.max_amount, daily_amount=data.daily_amount)
    db.add(rule)
    await audit(db,'limit_rule_created','limit_rule',actor.id,None,client_ip(request),{'merchant_id':str(data.merchant_id) if data.merchant_id else None,'method':data.method.value if data.method else None,'min_amount':str(data.min_amount),'max_amount':str(data.max_amount),'daily_amount':str(data.daily_amount)})
    await db.commit(); await db.refresh(rule)
    return rule

@router.get('/blacklist')
async def list_blacklist(db: AsyncSession=Depends(get_db), actor=Depends(require_roles(Role.superadmin, Role.admin, Role.support))):
    return (await db.execute(select(Blacklist).order_by(Blacklist.created_at.desc()).limit(300))).scalars().all()

@router.post('/blacklist')
async def upsert_blacklist(data: BlacklistIn, request: Request, db: AsyncSession=Depends(get_db), actor=Depends(require_roles(Role.superadmin, Role.admin))):
    item=(await db.execute(select(Blacklist).where(Blacklist.kind==data.kind.value, Blacklist.value==data.value))).scalar_one_or_none()
    if item:
        item.reason=data.reason
        item.is_active=data.is_active
        action='blacklist_updated'
    else:
        item=Blacklist(kind=data.kind.value, value=data.value, reason=data.reason, is_active=data.is_active)
        db.add(item)
        action='blacklist_created'
    await audit(db,action,'blacklist',actor.id,None,client_ip(request),{'kind':data.kind.value,'value':data.value,'is_active':data.is_active})
    await db.commit(); await db.refresh(item)
    return item

@router.get('/compliance/policy')
async def compliance_policy(actor=Depends(require_roles(Role.superadmin, Role.admin, Role.support))):
    return COMPLIANCE_POLICY

@router.get('/compliance/summary')
async def compliance_summary(db: AsyncSession=Depends(get_db), actor=Depends(require_roles(Role.superadmin, Role.admin, Role.support))):
    return await get_compliance_summary(db)

@router.get('/kyc-profiles')
async def kyc_profiles(
    db: AsyncSession=Depends(get_db),
    actor=Depends(require_roles(Role.superadmin, Role.admin, Role.support)),
    status: str | None = Query(default=None),
    risk_level: str | None = Query(default=None),
):
    profiles = await list_kyc_profiles(db, status=status, risk_level=risk_level)
    return [_kyc_public(profile) for profile in profiles]

@router.get('/merchants/{merchant_id}/compliance')
async def merchant_compliance(merchant_id: UUID, db: AsyncSession=Depends(get_db), actor=Depends(require_roles(Role.superadmin, Role.admin, Role.support))):
    try:
        snapshot = await merchant_compliance_snapshot(db, merchant_id=merchant_id)
    except ValueError:
        raise HTTPException(404, 'merchant not found')
    snapshot['kyc_profile'] = _kyc_public(snapshot['kyc_profile'])
    snapshot['recent_risk_events'] = [_risk_public(event) for event in snapshot['recent_risk_events']]
    return snapshot

@router.post('/kyc-profiles/{merchant_id}/review')
async def review_kyc(
    merchant_id: UUID,
    data: KycReviewIn,
    request: Request,
    db: AsyncSession=Depends(get_db),
    actor=Depends(require_roles(Role.superadmin, Role.admin)),
):
    if data.risk_level == 'prohibited' and data.status.value == 'approved':
        raise HTTPException(422, 'prohibited merchants cannot be approved')
    try:
        profile = await review_kyc_profile(
            db,
            merchant_id=merchant_id,
            status=data.status.value,
            risk_level=data.risk_level,
            legal_name=data.legal_name,
            tax_id=data.tax_id,
            country=data.country,
            reviewed_by=actor.id,
        )
    except ValueError as exc:
        raise HTTPException(404 if str(exc) == 'merchant not found' else 422, str(exc))
    await audit(
        db,
        'kyc_profile_reviewed',
        'kyc_profile',
        actor.id,
        profile.id,
        client_ip(request),
        {
            'merchant_id': str(merchant_id),
            'status': data.status.value,
            'risk_level': data.risk_level,
            'country': data.country,
            'comment': data.comment,
        },
    )
    await db.commit()
    await db.refresh(profile)
    return _kyc_public(profile)

@router.post('/deposits/{deposit_id}/confirm')
async def confirm_deposit(deposit_id: str, request: Request, db: AsyncSession=Depends(get_db), actor=Depends(require_roles(Role.superadmin))):
    try:
        result = await confirm_deposit_payment(
            db,
            deposit_id,
            actor_id=actor.id,
            actor_ip=client_ip(request),
            audit_action='deposit_confirmed',
            description='manual deposit confirmation',
        )
    except DepositConfirmationError as exc:
        raise HTTPException(exc.status_code, exc.api_detail) from exc
    delivery = enqueue_deposit_confirmation_deliveries(result)
    if not result.confirmed:
        raise HTTPException(
            409,
            {
                'code': 'deposit_expired',
                'reason': result.reason,
                'status': result.status,
            },
        )
    return {'ok':True,'webhook_event_id':result.webhook_event_id,**delivery,**result.settlement}

@router.post('/payouts/{payout_id}/complete')
async def complete_payout(payout_id: str, request: Request, db: AsyncSession=Depends(get_db), actor=Depends(require_roles(Role.superadmin))):
    p=(await db.execute(select(Payout).where(Payout.id==payout_id).with_for_update())).scalar_one_or_none()
    if not p: raise HTTPException(404,'not found')
    try:
        await complete_payout_flow(db, p)
    except ValueError as e:
        raise HTTPException(409, str(e))
    ev=await queue_webhook(db,p.merchant_id,'payout.completed',build_payout_webhook_payload(p, 'payout.completed'))
    await audit(db,'payout_completed','payout',actor.id,p.id,client_ip(request))
    await db.commit()
    webhook_enqueued = _enqueue_webhook(ev.id)
    return {'ok':True,'webhook_event_id':ev.id,'webhook_enqueued':webhook_enqueued}


@router.post('/payouts/{payout_id}/reject')
async def reject_payout(payout_id: str, request: Request, db: AsyncSession=Depends(get_db), actor=Depends(require_roles(Role.superadmin, Role.admin))):
    p=(await db.execute(select(Payout).where(Payout.id==payout_id).with_for_update())).scalar_one_or_none()
    if not p: raise HTTPException(404,'not found')
    try:
        await release_payout_hold(db, p, PayoutStatus.rejected, 'payout rejected by staff')
    except ValueError as e:
        raise HTTPException(409, str(e))
    ev=await queue_webhook(db,p.merchant_id,'payout.rejected',build_payout_webhook_payload(p, 'payout.rejected'))
    await audit(db,'payout_rejected','payout',actor.id,p.id,client_ip(request))
    await db.commit()
    webhook_enqueued = _enqueue_webhook(ev.id)
    return {'ok':True,'webhook_event_id':ev.id,'webhook_enqueued':webhook_enqueued}


@router.post('/payouts/{payout_id}/fail')
async def fail_payout(payout_id: str, request: Request, db: AsyncSession=Depends(get_db), actor=Depends(require_roles(Role.superadmin, Role.admin))):
    p=(await db.execute(select(Payout).where(Payout.id==payout_id).with_for_update())).scalar_one_or_none()
    if not p: raise HTTPException(404,'not found')
    try:
        await release_payout_hold(db, p, PayoutStatus.failed, 'payout failed by staff')
    except ValueError as e:
        raise HTTPException(409, str(e))
    ev=await queue_webhook(db,p.merchant_id,'payout.failed',build_payout_webhook_payload(p, 'payout.failed'))
    await audit(db,'payout_failed','payout',actor.id,p.id,client_ip(request))
    await db.commit()
    webhook_enqueued = _enqueue_webhook(ev.id)
    return {'ok':True,'webhook_event_id':ev.id,'webhook_enqueued':webhook_enqueued}

def _date_filters(model, date_from: datetime | None, date_to: datetime | None):
    filters = []
    if date_from:
        filters.append(model.created_at >= date_from)
    if date_to:
        filters.append(model.created_at <= date_to)
    return filters


@router.get('/stats')
async def stats(
    db: AsyncSession=Depends(get_db),
    actor=Depends(require_roles(Role.superadmin,Role.admin,Role.support)),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    merchant_id: UUID | None = Query(default=None),
    method: str | None = Query(default=None),
):
    if not can_view_admin_stats(actor):
        raise HTTPException(403, 'forbidden')
    dep_filters = _date_filters(Deposit, date_from, date_to)
    pay_filters = _date_filters(Payout, date_from, date_to)
    if merchant_id:
        dep_filters.append(Deposit.merchant_id == merchant_id)
        pay_filters.append(Payout.merchant_id == merchant_id)
    if method:
        dep_filters.append(Deposit.method == method)
        pay_filters.append(Payout.method == method)
    dep_total=(await db.execute(select(func.coalesce(func.sum(Deposit.amount),0)).where(Deposit.status==DepositStatus.paid.value,*dep_filters))).scalar_one()
    payout_total=(await db.execute(select(func.coalesce(func.sum(Payout.amount),0)).where(Payout.status==PayoutStatus.completed.value,*pay_filters))).scalar_one()
    dep_success=(await db.execute(select(func.count()).select_from(Deposit).where(Deposit.status==DepositStatus.paid.value,*dep_filters))).scalar_one()
    dep_failed=(await db.execute(select(func.count()).select_from(Deposit).where(Deposit.status.in_([DepositStatus.failed.value,DepositStatus.cancelled.value,DepositStatus.expired.value]),*dep_filters))).scalar_one()
    pay_success=(await db.execute(select(func.count()).select_from(Payout).where(Payout.status==PayoutStatus.completed.value,*pay_filters))).scalar_one()
    pay_failed=(await db.execute(select(func.count()).select_from(Payout).where(Payout.status.in_([PayoutStatus.failed.value,PayoutStatus.cancelled.value,PayoutStatus.rejected.value]),*pay_filters))).scalar_one()
    ledger_filters = _date_filters(LedgerEntry, date_from, date_to)
    if merchant_id:
        ledger_filters.append(LedgerEntry.merchant_id == merchant_id)
    platform_fee=(await db.execute(select(func.coalesce(func.sum(LedgerEntry.amount),0)).where(LedgerEntry.entry_type=='fee',*ledger_filters))).scalar_one()
    return {
        'turnover':dep_total,
        'payout_turnover':payout_total,
        'successful_deposits':dep_success,
        'failed_deposits':dep_failed,
        'successful_payouts':pay_success,
        'failed_payouts':pay_failed,
        'conversion': float(dep_success / max(dep_success+dep_failed,1)),
        'commission': platform_fee,
        'platform_income': platform_fee,
    }

@router.get('/audit')
async def audit_logs(db: AsyncSession=Depends(get_db), actor=Depends(require_roles(Role.superadmin))):
    rows=(await db.execute(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(100))).scalars().all()
    return rows

@router.get('/risk/events')
async def risk_events(db: AsyncSession=Depends(get_db), actor=Depends(require_roles(Role.superadmin,Role.admin,Role.support))):
    return (await db.execute(select(RiskEvent).order_by(RiskEvent.created_at.desc()).limit(200))).scalars().all()

@router.post('/sms/{sms_id}/confirm/{deposit_id}')
async def manual_sms_confirm(sms_id: UUID, deposit_id: UUID, request: Request, db: AsyncSession=Depends(get_db), actor=Depends(require_roles(Role.superadmin))):
    try:
        sms, dep, changed, finalization = await confirm_deposit_from_sms(
            db,
            sms_id,
            deposit_id,
            actor_id=actor.id,
            actor_ip=client_ip(request),
        )
    except LookupError:
        await db.rollback()
        raise HTTPException(404, 'sms or deposit not found')
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(409, str(exc)) from exc
    ev = None
    aggregator_callback_logs = []
    if changed:
        ev = await queue_webhook(db, dep.merchant_id, 'deposit.paid', build_deposit_webhook_payload(dep, 'deposit.paid', {'source':'sms'}))
        aggregator_callback_logs = await sync_payment_from_deposit(db, dep)
    await audit(db,'sms_manual_confirm','sms',actor.id,sms.id,client_ip(request),{'deposit_id':str(dep.id),'changed':changed})
    await db.commit()
    if finalization:
        if finalization.webhook_event_id:
            _enqueue_webhook(finalization.webhook_event_id)
        for callback_id in finalization.aggregator_callback_log_ids:
            enqueue_aggregator_callback_delivery(callback_id)
        raise HTTPException(409, 'deposit expired and was finalized as trader_timeout')
    webhook_enqueued = bool(ev and _enqueue_webhook(ev.id))
    aggregator_callbacks_enqueued = [enqueue_aggregator_callback_delivery(log.id) for log in aggregator_callback_logs]
    return {'ok':True,'sms_id':sms.id,'deposit_id':dep.id,'changed':changed,'webhook_event_id':ev.id if ev else None,'webhook_enqueued':webhook_enqueued,'aggregator_callbacks_enqueued':sum(1 for item in aggregator_callbacks_enqueued if item)}

@router.post('/appeals/{appeal_id}/messages')
async def add_appeal_message(appeal_id: UUID, data: AppealMessageIn, request: Request, db: AsyncSession=Depends(get_db), actor=Depends(require_roles(Role.superadmin,Role.admin,Role.support,Role.merchant,Role.operator,Role.trader))):
    appeal=(await db.execute(select(Appeal).where(Appeal.id==appeal_id))).scalar_one_or_none()
    if not appeal: raise HTTPException(404,'appeal not found')
    if not await can_message_appeal(db, actor, appeal):
        raise HTTPException(404, 'appeal not found')
    msg=AppealMessage(appeal_id=appeal.id, author_id=actor.id, message=data.message, attachment_path=data.attachment_path)
    appeal.status=AppealStatus.in_review.value if actor.role in [Role.superadmin.value, Role.admin.value, Role.support.value] else appeal.status
    db.add(msg)
    await audit(db,'appeal_message_added','appeal',actor.id,appeal.id,client_ip(request))
    await db.commit(); await db.refresh(msg)
    return msg

@router.post('/appeals/{appeal_id}/resolve')
async def resolve_appeal(appeal_id: UUID, data: AppealResolveIn, request: Request, db: AsyncSession=Depends(get_db), actor=Depends(require_roles(Role.superadmin,Role.admin))):
    appeal=(await db.execute(select(Appeal).where(Appeal.id==appeal_id))).scalar_one_or_none()
    if not appeal: raise HTTPException(404,'appeal not found')
    if data.status not in [AppealStatus.approved, AppealStatus.rejected, AppealStatus.returned_to_processing, AppealStatus.closed]:
        raise HTTPException(422,'invalid final status')
    try:
        event = await resolve_operation_appeal(db, appeal, status=data.status.value,
                                              actor_id=actor.id, decision=data.decision)
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(409, str(exc)) from exc
    if data.message:
        db.add(AppealMessage(appeal_id=appeal.id, author_id=actor.id, message=data.message))
    await audit(db,'appeal_resolved','appeal',actor.id,appeal.id,client_ip(request),{'status':appeal.status,'decision':appeal.decision})
    await db.commit()
    if event:
        _enqueue_webhook(event.id)
    return {'ok':True,'appeal_id':appeal.id,'status':appeal.status}

@router.post('/webhooks/{event_id}/retry')
async def admin_retry_webhook(event_id: UUID, request: Request, db: AsyncSession=Depends(get_db), actor=Depends(require_roles(Role.superadmin))):
    event = await request_manual_webhook_retry(db, event_id)
    if not event: raise HTTPException(404,'webhook event not found')
    await audit(
        db,
        'webhook_retry_requested',
        'webhook_event',
        actor.id,
        event.id,
        client_ip(request),
        {
            'event_id': str(event.id),
            'attempts': event.attempts,
            'max_attempts': event.max_attempts,
        },
    )
    await db.commit()
    webhook_enqueued = (
        False
        if event.status == 'delivered'
        else _enqueue_webhook(event.id)
    )
    return {'ok':True,'event_id':event.id,'status':event.status,'webhook_enqueued':webhook_enqueued}


@router.get('/merchants/{merchant_id}/webhook-signing-keys')
async def list_webhook_signing_keys(
    merchant_id: UUID,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_roles(Role.superadmin)),
):
    merchant = await db.scalar(
        select(Merchant).where(Merchant.id == merchant_id)
    )
    if not merchant:
        raise HTTPException(404, 'merchant not found')
    keys = (
        await db.execute(
            select(MerchantWebhookSigningKey)
            .where(
                MerchantWebhookSigningKey.merchant_id == merchant.id
            )
            .order_by(MerchantWebhookSigningKey.created_at.desc())
        )
    ).scalars().all()
    return {
        'merchant_id': merchant.id,
        'configuration_required': not any(
            key.status == 'active' for key in keys
        ),
        'keys': [
            {
                'key_id': key.key_id,
                'status': key.status,
                'created_at': key.created_at,
                'retire_at': key.retire_at,
                'revoked_at': key.revoked_at,
            }
            for key in keys
        ],
    }


@router.post('/merchants/{merchant_id}/webhook-signing-keys')
async def issue_merchant_webhook_signing_key(
    merchant_id: UUID,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_roles(Role.superadmin)),
):
    merchant = await db.scalar(
        select(Merchant)
        .where(Merchant.id == merchant_id)
        .with_for_update()
    )
    if not merchant:
        raise HTTPException(404, 'merchant not found')
    try:
        key, secret = await issue_webhook_signing_key(
            db,
            merchant,
            created_by=actor.id,
        )
    except WebhookSigningKeyError as exc:
        raise HTTPException(409, {'code': exc.code}) from exc
    await audit(
        db,
        'merchant_webhook_signing_key_issued',
        'merchant_webhook_signing_key',
        actor.id,
        key.id,
        client_ip(request),
        {
            'merchant_id': str(merchant.id),
            'key_fingerprint': webhook_key_fingerprint(key.key_id),
        },
    )
    await db.commit()
    response.headers['Cache-Control'] = 'no-store, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    return {
        'merchant_id': merchant.id,
        'key_id': key.key_id,
        'secret': secret,
        'secret_display': 'one_time',
    }


@router.post('/merchants/{merchant_id}/webhook-signing-keys/rotate')
async def rotate_merchant_webhook_signing_key(
    merchant_id: UUID,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_roles(Role.superadmin)),
):
    merchant = await db.scalar(
        select(Merchant)
        .where(Merchant.id == merchant_id)
        .with_for_update()
    )
    if not merchant:
        raise HTTPException(404, 'merchant not found')
    try:
        key, secret, retiring = await rotate_webhook_signing_key(
            db,
            merchant,
            created_by=actor.id,
        )
    except WebhookSigningKeyError as exc:
        raise HTTPException(409, {'code': exc.code}) from exc
    await audit(
        db,
        'merchant_webhook_signing_key_rotated',
        'merchant_webhook_signing_key',
        actor.id,
        key.id,
        client_ip(request),
        {
            'merchant_id': str(merchant.id),
            'new_key_fingerprint': webhook_key_fingerprint(key.key_id),
            'retiring_key_fingerprint': webhook_key_fingerprint(
                retiring.key_id
            ),
            'retire_at': retiring.retire_at.isoformat(),
        },
    )
    await db.commit()
    response.headers['Cache-Control'] = 'no-store, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    return {
        'merchant_id': merchant.id,
        'key_id': key.key_id,
        'secret': secret,
        'secret_display': 'one_time',
        'retiring_key_id': retiring.key_id,
        'retire_at': retiring.retire_at,
    }


@router.post(
    '/merchants/{merchant_id}/webhook-signing-keys/{key_id}/revoke'
)
async def revoke_merchant_webhook_signing_key(
    merchant_id: UUID,
    key_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_roles(Role.superadmin)),
):
    key = await db.scalar(
        select(MerchantWebhookSigningKey)
        .where(
            MerchantWebhookSigningKey.merchant_id == merchant_id,
            MerchantWebhookSigningKey.key_id == key_id,
        )
        .with_for_update()
    )
    if not key:
        raise HTTPException(404, 'webhook signing key not found')
    try:
        await revoke_webhook_signing_key(db, key)
    except WebhookSigningKeyError as exc:
        raise HTTPException(409, {'code': exc.code}) from exc
    await audit(
        db,
        'merchant_webhook_signing_key_revoked',
        'merchant_webhook_signing_key',
        actor.id,
        key.id,
        client_ip(request),
        {
            'merchant_id': str(merchant_id),
            'key_fingerprint': webhook_key_fingerprint(key.key_id),
        },
    )
    await db.commit()
    return {'ok': True, 'key_id': key.key_id, 'status': key.status}

@router.get('/reports/export')
async def export_operations_report(
    request: Request,
    db: AsyncSession=Depends(get_db),
    actor=Depends(require_roles(Role.superadmin,Role.admin)),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
):
    dep_filters = _date_filters(Deposit, date_from, date_to)
    pay_filters = _date_filters(Payout, date_from, date_to)
    deposits=(await db.execute(select(Deposit).where(*dep_filters).order_by(Deposit.created_at.desc()).limit(5000))).scalars().all()
    payouts=(await db.execute(select(Payout).where(*pay_filters).order_by(Payout.created_at.desc()).limit(5000))).scalars().all()
    buffer=io.StringIO()
    writer=csv.writer(buffer)
    writer.writerow(['type','id','merchant_id','external_id','amount','currency','method','status','created_at'])
    for d in deposits:
        writer.writerow(['deposit',d.id,d.merchant_id,d.external_id,d.amount,d.currency,d.method,d.status,d.created_at.isoformat()])
    for p in payouts:
        writer.writerow(['payout',p.id,p.merchant_id,p.external_id,p.amount,p.currency,p.method,p.status,p.created_at.isoformat()])
    report=ReportExport(actor_id=actor.id, report_type='operations_csv', status='completed', filters={'date_from':str(date_from) if date_from else None,'date_to':str(date_to) if date_to else None})
    db.add(report)
    await audit(db,'report_exported','report',actor.id,None,client_ip(request),report.filters)
    await db.commit()
    buffer.seek(0)
    return StreamingResponse(iter([buffer.getvalue()]), media_type='text/csv', headers={'Content-Disposition':'attachment; filename=operations_report.csv'})
