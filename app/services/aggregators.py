import json
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import DepositStatus, PaymentMethod, Role
from app.core.payment_methods import normalize_payment_method, payment_method_label
from app.core.requisite_providers import requisite_provider_payload
from app.core.security import decrypt_secret, encrypt_secret, reveal_text, sign_hmac
from app.core.validators import validate_public_webhook_url
from app.models import (
    AggregatorAccount,
    AggregatorCallbackLog,
    AggregatorMerchant,
    AggregatorPayment,
    Balance,
    Deposit,
    KycProfile,
    Merchant,
    PlatformLedgerEntry,
    Requisite,
    User,
)
from app.services.requisites import SelectedPaymentRequisite, select_deposit_requisite
from app.services.deposit_ttl import new_deposit_expires_at
from app.services.fee_tiers import (
    FeeConfigurationError,
    create_operation_fee_snapshot,
    get_operation_fee_snapshot,
    resolve_fee_rule,
    validate_margin,
)
from app.services.webhooks import _post_validated_webhook
from app.services.rapira import RollingRateUnavailable
from app.services.rolling import (
    RollingError,
    create_pending_allocation,
    rolling_quote_for_deposit_create,
)


AGGREGATOR_ACTIVE_STATUS = 'active'
AGGREGATOR_TERMINAL_STATUSES = {'paid', 'expired', 'cancelled', 'rejected', 'failed'}
CALLBACK_RETRY_DELAYS = [1, 5, 15, 60, 180]
CALLBACK_MAX_ATTEMPTS = 5
IDEMPOTENCY_CONFLICT = 'idempotency_conflict'


class AggregatorError(ValueError):
    pass


@dataclass
class AggregatorPaymentResult:
    payment: AggregatorPayment
    deposit: Deposit
    selected_requisite: SelectedPaymentRequisite | None
    idempotent: bool = False


def money(value: Decimal | int | str | None) -> Decimal:
    return Decimal(value or '0.00').quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def percent(value: Decimal | int | str | None) -> Decimal:
    raw = Decimal(value or '0.00')
    if raw < Decimal('0'):
        raw = Decimal('0')
    if raw > Decimal('100'):
        raw = Decimal('100')
    return raw.quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)


def generate_api_key(prefix: str = 'ak') -> str:
    return f'{prefix}_{secrets.token_urlsafe(24)}'


def generate_secret(prefix: str = 'sk') -> str:
    return f'{prefix}_{secrets.token_urlsafe(32)}'


def map_deposit_status(status: str) -> str:
    mapping = {
        DepositStatus.created.value: 'created',
        DepositStatus.pending.value: 'waiting_payment',
        DepositStatus.paid.value: 'paid',
        DepositStatus.failed.value: 'failed',
        DepositStatus.expired.value: 'expired',
        DepositStatus.cancelled.value: 'cancelled',
        DepositStatus.appeal_opened.value: 'waiting_payment',
    }
    return mapping.get(status, 'failed')


def callback_payload(payment: AggregatorPayment) -> dict:
    return {
        'event': 'payment.status_changed',
        'platform_payment_id': str(payment.platform_payment_id),
        'aggregator_order_id': payment.aggregator_order_id,
        'merchant_order_id': payment.merchant_order_id,
        'status': payment.status,
        'amount': str(money(payment.amount)),
        'currency': payment.currency,
        'paid_at': payment.paid_at.isoformat() if payment.paid_at else None,
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }


def merchant_callback_payload(payment: AggregatorPayment) -> dict:
    return {
        'event': 'payment.status_changed',
        'merchant_order_id': payment.merchant_order_id,
        'aggregator_order_id': payment.aggregator_order_id,
        'status': payment.status,
        'amount': str(money(payment.amount)),
        'currency': payment.currency,
        'paid_at': payment.paid_at.isoformat() if payment.paid_at else None,
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }


def payment_details_from_requisite(req: Requisite | None, dep: Deposit) -> dict | None:
    if not req:
        return None
    method = normalize_payment_method(req.method, canonical_mobile=True) or req.method
    provider = requisite_provider_payload(method, req.bank_code, req.operator_code, req.bank_name)
    requisite_value = reveal_text(req.value_encrypted)
    return {
        'type': 'card' if method == PaymentMethod.c2c.value else method,
        'method': method,
        'payment_method': method,
        'method_label': payment_method_label(method),
        'bank': provider.get('bank', ''),
        'holder_name': req.full_name or req.owner_name,
        'card_number': requisite_value if method == PaymentMethod.c2c.value else '',
        'phone': requisite_value if method in {PaymentMethod.sbp.value, PaymentMethod.mobile_commerce.value} else None,
        'requisite': requisite_value,
        'exact_amount': str(money(dep.amount)),
        **provider,
    }


def _payment_metadata(value: AggregatorPayment | Deposit | None) -> dict:
    metadata = getattr(value, 'metadata_json', None)
    return metadata if isinstance(metadata, dict) else {}


def _same_aggregator_payment(
    payment: AggregatorPayment,
    dep: Deposit,
    *,
    aggregator_order_id: str,
    merchant_order_id: str,
    external_merchant_id: str,
    amount: Decimal,
    currency: str,
    payment_method: str,
    callback_url: str | None,
    success_url: str | None,
    fail_url: str | None,
) -> bool:
    dep_metadata = _payment_metadata(dep)
    payment_metadata = _payment_metadata(payment)
    return (
        payment.aggregator_order_id == aggregator_order_id
        and payment.merchant_order_id == merchant_order_id
        and dep_metadata.get('external_merchant_id') == external_merchant_id
        and money(payment.amount) == money(amount)
        and payment.currency == currency
        and payment.payment_method == payment_method
        and payment_metadata.get('callback_url') == callback_url
        and payment_metadata.get('success_url') == success_url
        and payment_metadata.get('fail_url') == fail_url
    )


async def _payment_by_deposit_id(db: AsyncSession, aggregator: AggregatorAccount, deposit_id: UUID) -> AggregatorPayment | None:
    return (
        await db.execute(
            select(AggregatorPayment).where(
                AggregatorPayment.aggregator_id == aggregator.id,
                AggregatorPayment.platform_payment_id == deposit_id,
            )
        )
    ).scalar_one_or_none()


async def _deposit_by_payment(db: AsyncSession, payment: AggregatorPayment) -> Deposit:
    return (
        await db.execute(select(Deposit).where(Deposit.id == payment.platform_payment_id))
    ).scalar_one()


async def _existing_aggregator_result(
    db: AsyncSession,
    *,
    aggregator: AggregatorAccount,
    idempotency_key: str,
    aggregator_order_id: str,
    merchant_order_id: str,
    external_merchant_id: str,
    amount: Decimal,
    currency: str,
    payment_method: str,
    callback_url: str | None,
    success_url: str | None,
    fail_url: str | None,
) -> AggregatorPaymentResult | None:
    dep_by_idempotency = (
        await db.execute(
            select(Deposit).where(
                Deposit.merchant_id == aggregator.platform_merchant_id,
                Deposit.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if dep_by_idempotency:
        payment = await _payment_by_deposit_id(db, aggregator, dep_by_idempotency.id)
        if not payment:
            raise AggregatorError(IDEMPOTENCY_CONFLICT)
        if not _same_aggregator_payment(
            payment,
            dep_by_idempotency,
            aggregator_order_id=aggregator_order_id,
            merchant_order_id=merchant_order_id,
            external_merchant_id=external_merchant_id,
            amount=amount,
            currency=currency,
            payment_method=payment_method,
            callback_url=callback_url,
            success_url=success_url,
            fail_url=fail_url,
        ):
            raise AggregatorError(IDEMPOTENCY_CONFLICT)
        return AggregatorPaymentResult(payment, dep_by_idempotency, None, idempotent=True)

    by_aggregator_order = (
        await db.execute(
            select(AggregatorPayment).where(
                AggregatorPayment.aggregator_id == aggregator.id,
                AggregatorPayment.aggregator_order_id == aggregator_order_id,
            )
        )
    ).scalar_one_or_none()
    by_merchant_order = (
        await db.execute(
            select(AggregatorPayment).where(
                AggregatorPayment.aggregator_id == aggregator.id,
                AggregatorPayment.merchant_order_id == merchant_order_id,
            )
        )
    ).scalar_one_or_none()
    if by_aggregator_order and by_merchant_order and by_aggregator_order.id != by_merchant_order.id:
        raise AggregatorError(IDEMPOTENCY_CONFLICT)
    payment = by_aggregator_order or by_merchant_order
    if not payment:
        return None
    dep = await _deposit_by_payment(db, payment)
    if not _same_aggregator_payment(
        payment,
        dep,
        aggregator_order_id=aggregator_order_id,
        merchant_order_id=merchant_order_id,
        external_merchant_id=external_merchant_id,
        amount=amount,
        currency=currency,
        payment_method=payment_method,
        callback_url=callback_url,
        success_url=success_url,
        fail_url=fail_url,
    ):
        raise AggregatorError(IDEMPOTENCY_CONFLICT)
    return AggregatorPaymentResult(payment, dep, None, idempotent=True)


async def create_aggregator_account(
    db: AsyncSession,
    *,
    name: str,
    callback_url: str | None = None,
    success_url: str | None = None,
    fail_url: str | None = None,
    commission_percent: Decimal = Decimal('0.00'),
    min_payment_amount: Decimal = Decimal('100.00'),
    max_payment_amount: Decimal = Decimal('150000.00'),
    daily_limit: Decimal | None = None,
    monthly_limit: Decimal | None = None,
) -> tuple[AggregatorAccount, str]:
    existing = (await db.execute(select(AggregatorAccount).where(AggregatorAccount.name == name))).scalar_one_or_none()
    if existing:
        raise AggregatorError('aggregator name already exists')

    from app.services.integration_modes import verify_environment
    from app.models import AggregatorApiKey
    mode = await verify_environment(db)
    # Production creation registers the account only. Key issue is a separate,
    # owner-gated Superadmin command. This marker is never accepted by API auth.
    api_key = generate_api_key('ak_test' if mode=='sandbox' else 'unissued')
    secret = generate_secret() if mode=='sandbox' else ''
    user = User(
        email=f'{api_key}@aggregator.local',
        password_hash='disabled-aggregator-account',
        role=Role.aggregator.value,
        is_active=False,
    )
    db.add(user)
    await db.flush()

    platform_merchant = Merchant(
        owner_id=user.id,
        name=f'Aggregator: {name}',
        webhook_url=None,
        ip_whitelist=[],
        sandbox_mode=False,
        merchant_commission_percent=Decimal('0.00'),
    )
    db.add(platform_merchant)
    await db.flush()
    db.add(Balance(merchant_id=platform_merchant.id, available=Decimal('0.00')))
    db.add(KycProfile(merchant_id=platform_merchant.id, status='not_started', risk_level='standard'))

    account = AggregatorAccount(
        platform_merchant_id=platform_merchant.id,
        name=name,
        api_key=api_key,
        secret_hash=encrypt_secret(secret),
        callback_url=callback_url,
        success_url=success_url,
        fail_url=fail_url,
        commission_percent=percent(commission_percent),
        min_payment_amount=money(min_payment_amount),
        max_payment_amount=money(max_payment_amount),
        daily_limit=money(daily_limit) if daily_limit is not None else None,
        monthly_limit=money(monthly_limit) if monthly_limit is not None else None,
    )
    db.add(account)
    await db.flush()
    if mode == 'sandbox':
        db.add(AggregatorApiKey(aggregator_id=account.id, api_key=api_key,
            encrypted_secret=account.secret_hash, mode='sandbox', status='active'))
        await db.flush()
    return account, secret


async def regenerate_aggregator_secret(db: AsyncSession, aggregator: AggregatorAccount) -> str:
    # The old public browser command is retained but must use the audited,
    # actor-aware credential command. There is no unguarded issuance backdoor.
    raise AggregatorError('Use the confirmed aggregator credential rotation command')


async def get_or_create_aggregator_merchant(
    db: AsyncSession,
    *,
    aggregator: AggregatorAccount,
    external_merchant_id: str,
    merchant_name: str | None = None,
    callback_url: str | None = None,
    merchant_secret_key: str | None = None,
) -> tuple[AggregatorMerchant, str | None]:
    merchant = (await db.execute(
        select(AggregatorMerchant).where(
            AggregatorMerchant.aggregator_id == aggregator.id,
            AggregatorMerchant.external_merchant_id == external_merchant_id,
        ).with_for_update()
    )).scalar_one_or_none()
    if merchant:
        if callback_url:
            merchant.merchant_callback_url = callback_url
        return merchant, None
    secret = merchant_secret_key or generate_secret('msk')
    merchant = AggregatorMerchant(
        aggregator_id=aggregator.id,
        external_merchant_id=external_merchant_id,
        merchant_name=merchant_name or external_merchant_id,
        merchant_callback_url=callback_url,
        merchant_secret_hash=encrypt_secret(secret),
    )
    db.add(merchant)
    await db.flush()
    return merchant, secret


async def validate_aggregator_limits(db: AsyncSession, aggregator: AggregatorAccount, amount: Decimal) -> None:
    amount = money(amount)
    if aggregator.status != AGGREGATOR_ACTIVE_STATUS or getattr(aggregator, 'is_archived', False):
        raise AggregatorError('aggregator is not active')
    if amount < money(aggregator.min_payment_amount) or amount > money(aggregator.max_payment_amount):
        raise AggregatorError('amount is outside aggregator limits')

    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    counted_statuses = ['created', 'requisites_issued', 'waiting_payment', 'paid']
    if aggregator.daily_limit and aggregator.daily_limit > 0:
        today_sum = (await db.execute(
            select(func.coalesce(func.sum(AggregatorPayment.amount), 0)).where(
                AggregatorPayment.aggregator_id == aggregator.id,
                AggregatorPayment.status.in_(counted_statuses),
                AggregatorPayment.created_at >= day_start,
            )
        )).scalar_one()
        if money(today_sum) + amount > money(aggregator.daily_limit):
            raise AggregatorError('aggregator daily limit exceeded')
    if aggregator.monthly_limit and aggregator.monthly_limit > 0:
        month_sum = (await db.execute(
            select(func.coalesce(func.sum(AggregatorPayment.amount), 0)).where(
                AggregatorPayment.aggregator_id == aggregator.id,
                AggregatorPayment.status.in_(counted_statuses),
                AggregatorPayment.created_at >= month_start,
            )
        )).scalar_one()
        if money(month_sum) + amount > money(aggregator.monthly_limit):
            raise AggregatorError('aggregator monthly limit exceeded')


async def create_payment(
    db: AsyncSession,
    *,
    aggregator: AggregatorAccount,
    aggregator_order_id: str,
    merchant_order_id: str,
    external_merchant_id: str,
    amount: Decimal,
    currency: str,
    payment_method: str,
    client_id: str | None,
    client_ip: str | None,
    callback_url: str | None,
    success_url: str | None,
    fail_url: str | None,
    idempotency_key: str,
) -> AggregatorPaymentResult:
    amount = money(amount)
    callback_url = callback_url or aggregator.callback_url
    if callback_url:
        callback_url = validate_public_webhook_url(callback_url)
    if success_url:
        success_url = validate_public_webhook_url(success_url)
    if fail_url:
        fail_url = validate_public_webhook_url(fail_url)

    existing = await _existing_aggregator_result(
        db,
        aggregator=aggregator,
        idempotency_key=idempotency_key,
        aggregator_order_id=aggregator_order_id,
        merchant_order_id=merchant_order_id,
        external_merchant_id=external_merchant_id,
        amount=amount,
        currency=currency,
        payment_method=payment_method,
        callback_url=callback_url,
        success_url=success_url,
        fail_url=fail_url,
    )
    if existing:
        return existing

    await validate_aggregator_limits(db, aggregator, amount)
    merchant_rate_rule = await resolve_fee_rule(
        db,
        entity_type='merchant',
        entity_id=aggregator.platform_merchant_id,
        fee_side='merchant_fee',
        payment_method=payment_method,
        currency=currency,
        amount=amount,
    )
    aggregator_rate_rule = await resolve_fee_rule(
        db,
        entity_type='aggregator',
        entity_id=aggregator.id,
        fee_side='executor_fee',
        payment_method=payment_method,
        currency=currency,
        amount=amount,
    )
    validate_margin(merchant_rate_rule, aggregator_rate_rule)
    internal_merchant, _ = await get_or_create_aggregator_merchant(
        db,
        aggregator=aggregator,
        external_merchant_id=external_merchant_id,
        merchant_name=external_merchant_id,
        callback_url=callback_url,
    )
    if internal_merchant.status != AGGREGATOR_ACTIVE_STATUS:
        raise AggregatorError('aggregator merchant is not active')

    deposit_created_at = datetime.now(timezone.utc)
    try:
        rolling_quote = await rolling_quote_for_deposit_create(
            db,
            aggregator.platform_merchant_id,
            created_at=deposit_created_at,
        )
    except RollingRateUnavailable as exc:
        raise AggregatorError(exc.code) from exc
    except RollingError as exc:
        raise AggregatorError(getattr(exc, 'code', 'rolling_error')) from exc

    selected = await select_deposit_requisite(
        db,
        merchant_id=aggregator.platform_merchant_id,
        method=payment_method,
        amount=amount,
        merchant_rate_rule=merchant_rate_rule,
        executor_type='aggregator',
        executor_rule=aggregator_rate_rule,
        executor_id=aggregator.id,
    )
    dep = Deposit(
        merchant_id=aggregator.platform_merchant_id,
        external_id=aggregator_order_id,
        idempotency_key=idempotency_key,
        amount=amount,
        currency=currency,
        method=payment_method,
        status=DepositStatus.pending.value,
        requisites_id=selected.requisite.id,
        client_ip=client_ip,
        created_at=deposit_created_at,
        expires_at=new_deposit_expires_at(),
        metadata_json={
            **selected.settlement_metadata(),
            'source': 'aggregator',
            'aggregator_id': str(aggregator.id),
            'aggregator_merchant_id': str(internal_merchant.id),
            'aggregator_order_id': aggregator_order_id,
            'merchant_order_id': merchant_order_id,
            'external_merchant_id': external_merchant_id,
            'success_url': success_url,
            'fail_url': fail_url,
        },
    )
    db.add(dep)
    try:
        await db.flush()
        snapshot = await create_operation_fee_snapshot(
            db,
            deposit=dep,
            merchant_rule=merchant_rate_rule,
            executor_rule=aggregator_rate_rule,
            executor_type='aggregator',
            executor_id=aggregator.id,
        )
        await create_pending_allocation(
            db,
            deposit=dep,
            snapshot=snapshot,
            quote=rolling_quote,
        )
    except RollingError as exc:
        await db.rollback()
        raise AggregatorError(getattr(exc, 'code', 'rolling_error')) from exc
    except IntegrityError as exc:
        await db.rollback()
        existing = await _existing_aggregator_result(
            db,
            aggregator=aggregator,
            idempotency_key=idempotency_key,
            aggregator_order_id=aggregator_order_id,
            merchant_order_id=merchant_order_id,
            external_merchant_id=external_merchant_id,
            amount=amount,
            currency=currency,
            payment_method=payment_method,
            callback_url=callback_url,
            success_url=success_url,
            fail_url=fail_url,
        )
        if existing:
            return existing
        raise AggregatorError(IDEMPOTENCY_CONFLICT) from exc
    payment = AggregatorPayment(
        aggregator_id=aggregator.id,
        aggregator_merchant_id=internal_merchant.id,
        merchant_order_id=merchant_order_id,
        aggregator_order_id=aggregator_order_id,
        platform_payment_id=dep.id,
        amount=amount,
        currency=currency,
        payment_method=payment_method,
        status='waiting_payment',
        client_id=client_id,
        client_ip=client_ip,
        callback_url_to_merchant=callback_url or internal_merchant.merchant_callback_url,
        expires_at=dep.expires_at,
        metadata_json={'callback_url': callback_url, 'success_url': success_url, 'fail_url': fail_url},
    )
    db.add(payment)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        existing = await _existing_aggregator_result(
            db,
            aggregator=aggregator,
            idempotency_key=idempotency_key,
            aggregator_order_id=aggregator_order_id,
            merchant_order_id=merchant_order_id,
            external_merchant_id=external_merchant_id,
            amount=amount,
            currency=currency,
            payment_method=payment_method,
            callback_url=callback_url,
            success_url=success_url,
            fail_url=fail_url,
        )
        if existing:
            return existing
        raise AggregatorError(IDEMPOTENCY_CONFLICT) from exc
    return AggregatorPaymentResult(payment, dep, selected, idempotent=False)


async def payment_response(db: AsyncSession, result: AggregatorPaymentResult) -> dict:
    payment = result.payment
    dep = result.deposit
    req = None
    if dep.requisites_id:
        req = (await db.execute(select(Requisite).where(Requisite.id == dep.requisites_id))).scalar_one_or_none()
    details = result.selected_requisite.payment_details() if result.selected_requisite else payment_details_from_requisite(req, dep)
    return {
        'status': 'success',
        'platform_payment_id': str(payment.platform_payment_id),
        'aggregator_order_id': payment.aggregator_order_id,
        'merchant_order_id': payment.merchant_order_id,
        'amount': str(money(payment.amount)),
        'currency': payment.currency,
        'payment_method': payment.payment_method,
        'requisites': {
            'type': details.get('method') if details else payment.payment_method,
            'method_label': details.get('method_label') if details else payment_method_label(payment.payment_method),
            'bank': details.get('bank') if details else '',
            'bank_code': details.get('bank_code') if details else '',
            'operator': details.get('operator') if details else '',
            'operator_code': details.get('operator_code') if details else '',
            'provider_type': details.get('provider_type') if details else '',
            'provider_name': details.get('provider_name') if details else '',
            'holder_name': (details.get('receiver_name') or details.get('holder_name')) if details else '',
            'card_number': details.get('requisite') if details and payment.payment_method == PaymentMethod.c2c.value else '',
            'phone': details.get('requisite') if details and payment.payment_method in {PaymentMethod.sbp.value, PaymentMethod.mobile.value, PaymentMethod.mobile_commerce.value} else None,
            'requisite': details.get('requisite') if details else '',
            'exact_amount': details.get('exact_amount') if details else str(money(payment.amount)),
        },
        'expires_at': payment.expires_at.isoformat(),
        'payment_status': payment.status,
        'idempotent': result.idempotent,
    }


async def sync_payment_from_deposit(db: AsyncSession, dep: Deposit) -> list[AggregatorCallbackLog]:
    payment = (await db.execute(
        select(AggregatorPayment)
        .where(AggregatorPayment.platform_payment_id == dep.id)
        .with_for_update()
    )).scalar_one_or_none()
    if not payment:
        return []
    old_status = payment.status
    new_status = map_deposit_status(dep.status)
    payment.status = new_status
    if new_status == 'paid' and not payment.paid_at:
        payment.paid_at = datetime.now(timezone.utc)
        await accrue_paid_payment(db, payment)
    if old_status == new_status and payment.callback_status in {'sent', 'pending'}:
        return []
    if new_status not in AGGREGATOR_TERMINAL_STATUSES:
        return []
    return await queue_payment_callbacks(db, payment)


async def accrue_paid_payment(db: AsyncSession, payment: AggregatorPayment) -> None:
    aggregator = (await db.execute(
        select(AggregatorAccount).where(AggregatorAccount.id == payment.aggregator_id).with_for_update()
    )).scalar_one()
    snapshot = await get_operation_fee_snapshot(db, payment.platform_payment_id, lock=True)
    if snapshot.executor_type != 'aggregator' or snapshot.executor_id != aggregator.id:
        raise FeeConfigurationError('aggregator payment executor does not match fee snapshot')
    amount = money(payment.amount)
    aggregator_fee = money(snapshot.executor_fee_amount)
    merchant_amount = money(amount - aggregator_fee)
    aggregator.balance = money(aggregator.balance) + aggregator_fee
    aggregator.total_turnover = money(aggregator.total_turnover) + amount
    meta = dict(payment.metadata_json or {})
    meta.update({
        'aggregator_commission_amount': str(aggregator_fee),
        'merchant_amount': str(merchant_amount),
        'aggregator_commission_percent': str(snapshot.executor_rate_percent),
        'executor_rate_rule_id': str(snapshot.executor_rate_rule_id),
        'fee_snapshot_id': str(snapshot.id),
    })
    payment.metadata_json = meta


async def queue_payment_callbacks(db: AsyncSession, payment: AggregatorPayment) -> list[AggregatorCallbackLog]:
    aggregator = (await db.execute(select(AggregatorAccount).where(AggregatorAccount.id == payment.aggregator_id))).scalar_one()
    internal_merchant = None
    if payment.aggregator_merchant_id:
        internal_merchant = (await db.execute(
            select(AggregatorMerchant).where(AggregatorMerchant.id == payment.aggregator_merchant_id)
        )).scalar_one_or_none()
    logs: list[AggregatorCallbackLog] = []
    if aggregator.callback_url:
        logs.append(AggregatorCallbackLog(
            direction='platform_to_aggregator',
            related_payment_id=payment.id,
            target_url=aggregator.callback_url,
            payload_json=callback_payload(payment),
        ))
    merchant_url = payment.callback_url_to_merchant or (internal_merchant.merchant_callback_url if internal_merchant else None)
    if merchant_url:
        logs.append(AggregatorCallbackLog(
            direction='aggregator_to_merchant',
            related_payment_id=payment.id,
            target_url=merchant_url,
            payload_json=merchant_callback_payload(payment),
        ))
    for log in logs:
        db.add(log)
    if logs:
        payment.callback_status = 'pending'
        await db.flush()
    return logs


async def deliver_callback_log(db: AsyncSession, log_id: UUID) -> AggregatorCallbackLog:
    if not isinstance(log_id, UUID):
        log_id = UUID(str(log_id))
    log = (await db.execute(
        select(AggregatorCallbackLog)
        .where(AggregatorCallbackLog.id == log_id)
        .with_for_update()
    )).scalar_one()
    now = datetime.now(timezone.utc)
    if log.status in {'sent', 'configuration_error'}:
        return log
    if log.status == 'delivering' and (
        not log.next_retry_at or log.next_retry_at > now
    ):
        return log
    if log.status in {'pending', 'failed'} and log.next_retry_at and log.next_retry_at > now:
        return log
    if log.attempt >= CALLBACK_MAX_ATTEMPTS:
        log.status = 'failed'
        log.next_retry_at = None
        return log
    payment = (await db.execute(select(AggregatorPayment).where(AggregatorPayment.id == log.related_payment_id))).scalar_one()
    aggregator = (await db.execute(select(AggregatorAccount).where(AggregatorAccount.id == payment.aggregator_id))).scalar_one()
    secret = decrypt_secret(aggregator.secret_hash)
    if log.direction == 'aggregator_to_merchant' and payment.aggregator_merchant_id:
        merchant = (await db.execute(select(AggregatorMerchant).where(AggregatorMerchant.id == payment.aggregator_merchant_id))).scalar_one_or_none()
        if merchant:
            secret = decrypt_secret(merchant.merchant_secret_hash)
    if not secret:
        log.status = 'configuration_error'
        log.error_message = 'missing_callback_secret'
        payment.last_callback_error = log.error_message
        if log.direction == 'aggregator_to_merchant':
            payment.callback_status = log.status
        return log
    body = json.dumps(log.payload_json, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    timestamp = str(int(time.time()))
    delivery_id = str(uuid4())
    headers = {
        'Content-Type': 'application/json',
        'X-Timestamp': timestamp,
        'X-Signature': sign_hmac(secret, timestamp, body),
        'X-Request-ID': str(log.id),
        'X-Delivery-ID': delivery_id,
        'X-Idempotency-Key': f'aggregator-callback:{log.id}',
    }
    log.status = 'delivering'
    log.attempt += 1
    payment.callback_attempts += 1
    log.next_retry_at = now + timedelta(minutes=2)
    claim_attempt = log.attempt
    target_url = log.target_url
    log_id = log.id
    payment_id = payment.id
    await db.flush()
    # Release the callback/payment row locks before DNS and HTTP. The lease is
    # reclaimed by the periodic scanner if the worker dies after this commit.
    await db.commit()

    status_code = None
    final_error = None
    try:
        http_result = await _post_validated_webhook(
            target_url,
            body,
            headers,
            timeout_seconds=10.0,
        )
        status_code = http_result.status_code
        if not 200 <= status_code < 300:
            raise AggregatorError(f'HTTP {status_code}')
    except Exception as exc:
        final_error = str(exc)[:1000]

    log = (await db.execute(
        select(AggregatorCallbackLog)
        .where(AggregatorCallbackLog.id == log_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one()
    payment = (await db.execute(
        select(AggregatorPayment)
        .where(AggregatorPayment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one()
    if log.status != 'delivering' or log.attempt != claim_attempt:
        return log

    log.response_status_code = status_code
    log.response_body = ''
    if final_error is None:
        log.status = 'sent'
        log.error_message = None
        log.next_retry_at = None
        log.sent_at = datetime.now(timezone.utc)
        if log.direction == 'aggregator_to_merchant':
            payment.callback_status = 'sent'
    else:
        log.status = (
            'failed'
            if log.attempt >= CALLBACK_MAX_ATTEMPTS
            else 'pending'
        )
        log.error_message = final_error
        payment.last_callback_error = final_error
        if log.direction == 'aggregator_to_merchant':
            payment.callback_status = log.status
        if log.status == 'pending':
            delay = CALLBACK_RETRY_DELAYS[
                min(log.attempt - 1, len(CALLBACK_RETRY_DELAYS) - 1)
            ]
            log.next_retry_at = datetime.now(timezone.utc) + timedelta(
                minutes=delay
            )
        else:
            log.next_retry_at = None
    return log
