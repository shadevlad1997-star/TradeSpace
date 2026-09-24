from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import AppealStatus, DepositStatus, PayoutStatus, Role
from app.models import Appeal, AppealMessage, Deposit, Payout, Requisite, User
from app.services.antiscam import check_payment_result_for_risk
from app.services.fees import settle_deposit_credit
from app.services.ledger import LedgerError, trader_debit_available, trader_debit_frozen, trader_release_hold
from app.services.fee_tiers import get_operation_fee_snapshot
from app.services.aggregators import sync_payment_from_deposit
from app.services.audit import audit
from app.services.rolling import reopen_released_allocation
from app.services.webhooks import queue_webhook
from app.services.webhook_payloads import build_deposit_webhook_payload


APPEAL_REVIEW_TTL = timedelta(minutes=90)
APPEAL_ACTIVE_STATUSES = {AppealStatus.opened.value, AppealStatus.in_review.value}
TRADER_REJECTION_REASONS = {
    'wrong_requisite': 'Неверные реквизиты',
    'wrong_amount': 'Неверная сумма',
    'no_payment': 'Отсутствие платежа',
}


def money(value: Decimal | int | str | None) -> Decimal:
    return Decimal(value or '0.00').quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def metadata(appeal: Appeal) -> dict:
    return dict(appeal.metadata_json or {})


def set_metadata(appeal: Appeal, **values) -> dict:
    meta = metadata(appeal)
    meta.update(values)
    appeal.metadata_json = meta
    return meta


def default_deadline() -> datetime:
    return utcnow() + APPEAL_REVIEW_TTL


def appeal_deadline(appeal: Appeal) -> datetime:
    meta = metadata(appeal)
    raw = meta.get('deadline_at')
    if raw:
        try:
            return aware(datetime.fromisoformat(str(raw)))
        except ValueError:
            pass
    return aware(appeal.created_at) + APPEAL_REVIEW_TTL


def appeal_remaining_seconds(appeal: Appeal) -> int:
    if appeal.status not in APPEAL_ACTIVE_STATUSES:
        return 0
    if metadata(appeal).get('trader_decision') == 'rejected':
        return 0
    return max(0, int((appeal_deadline(appeal) - utcnow()).total_seconds()))


def deposit_payload(dep: Deposit, appeal: Appeal | None = None) -> dict:
    extra = {'appeal_id': str(appeal.id)} if appeal else None
    return build_deposit_webhook_payload(dep, extra=extra)


async def resolve_deposit_trader(db: AsyncSession, dep: Deposit, trader_id: str | UUID | None = None) -> tuple[Requisite | None, User | None]:
    req = None
    if dep.requisites_id:
        req = (await db.execute(select(Requisite).where(Requisite.id == dep.requisites_id).with_for_update())).scalar_one_or_none()
    selected_trader_id = trader_id or (req.trader_id if req else None)
    if not selected_trader_id:
        return req, None
    trader = (await db.execute(
        select(User)
        .where(User.id == UUID(str(selected_trader_id)), User.role.in_([Role.operator.value, Role.trader.value]))
        .with_for_update()
    )).scalar_one_or_none()
    return req, trader


async def settle_trader_for_approved_appeal(db: AsyncSession, dep: Deposit, trader_id: str | UUID | None = None) -> User | None:
    req, trader = await resolve_deposit_trader(db, dep, trader_id)
    if not trader:
        raise ValueError('appeal trader assignment is missing; manual review required')

    snapshot = await get_operation_fee_snapshot(db, dep.id, lock=True)
    if money(snapshot.calculation_base_amount) != money(dep.amount) or snapshot.currency != dep.currency or snapshot.payment_method != dep.method:
        raise ValueError('appeal fee snapshot does not match operation')
    if snapshot.settlement_status == 'settled':
        raise ValueError('appeal payment is already financially settled; manual review required')
    meta = dict(dep.metadata_json or {})
    if meta.get('trader_settled') is True or meta.get('trader_hold_status') == 'settled':
        raise ValueError('appeal trader is already settled but payment is not paid; manual review required')
    if req and req.trader_id != trader.id:
        raise ValueError('appeal trader does not match operation assignment')
    # The executor fee is the trader fee ONLY for a trader-executed snapshot.
    # Aggregator snapshots pay the aggregator, and historically reserve/debit
    # the trader's full gross. Do not transfer that fee to the trader by mistake.
    if snapshot.executor_type == 'trader':
        if snapshot.executor_id != trader.id:
            raise ValueError('appeal trader does not match fee snapshot')
        trader_fee = money(snapshot.executor_fee_amount)
    elif snapshot.executor_type == 'aggregator':
        if money(meta.get('trader_profit_amount')) != 0:
            raise ValueError('ambiguous aggregator trader commission; manual review required')
        trader_fee = Decimal('0.00')
    else:
        raise ValueError('unsupported appeal executor')
    if trader_fee < 0 or trader_fee > money(dep.amount):
        raise ValueError('invalid saved trader commission')
    settlement_amount = money(dep.amount) - trader_fee
    hold_status = meta.get('trader_hold_status')
    if hold_status not in {'active','released'} or meta.get('trader_hold_amount') is None:
        raise ValueError('appeal reserve history is missing; manual review required')
    hold_amount = money(meta['trader_hold_amount'])

    if hold_status == 'active':
        if hold_amount < settlement_amount or money(trader.trader_hold) < hold_amount:
            raise ValueError('appeal reserve is insufficient or inconsistent')
        try:
            if settlement_amount > 0:
                await trader_debit_frozen(
                    db,
                    trader,
                    settlement_amount,
                    dep.id,
                    f'trader-appeal-settle:{dep.id}',
                    'appeal approved deposit settlement debit',
                )
            extra_hold = money(hold_amount - settlement_amount)
            if extra_hold > 0:
                await trader_release_hold(
                    db,
                    trader,
                    extra_hold,
                    dep.id,
                    f'trader-appeal-profit-release:{dep.id}',
                    'appeal approved trader commission release',
                )
        except LedgerError as exc:
            raise ValueError(str(exc)) from exc
    elif hold_status != 'released':
        raise ValueError(f'trader hold status {hold_status} cannot be settled by appeal')
    elif settlement_amount > 0:
        try:
            await trader_debit_available(
                db,
                trader,
                settlement_amount,
                dep.id,
                f'trader-appeal-available-debit:{dep.id}',
                'appeal approved deposit settlement debit',
            )
        except LedgerError as exc:
            raise ValueError(str(exc)) from exc
    meta.update({
        'trader_hold_status': 'settled',
        'trader_settled': True,
        'trader_settled_at': utcnow().isoformat(),
        'trader_settlement_source': 'appeal',
        'trader_settlement_amount': str(settlement_amount),
    })
    dep.metadata_json = meta
    return trader


async def _lock_deposit_then_appeal(
    db: AsyncSession,
    appeal: Appeal,
) -> tuple[Deposit, Appeal]:
    """Use the global Deposit -> Appeal lock order for every resolution."""
    appeal_id = appeal.id
    operation_id = appeal.operation_id
    dep = (await db.execute(
        select(Deposit)
        .where(Deposit.id == operation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if not dep:
        raise ValueError('deposit not found')
    locked_appeal = (await db.execute(
        select(Appeal)
        .where(Appeal.id == appeal_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if (
        not locked_appeal
        or locked_appeal.operation_type != 'deposit'
        or locked_appeal.operation_id != dep.id
    ):
        raise ValueError('appeal target changed while it was being resolved')
    return dep, locked_appeal


async def approve_deposit_appeal(db: AsyncSession, appeal: Appeal, *, actor_id=None, automatic: bool = False) -> object | None:
    # A background batch catches business errors and continues. Keep EACH
    # resolution atomic even when the caller does not roll back the batch.
    async with db.begin_nested():
        return await _approve_deposit_appeal(db, appeal, actor_id=actor_id, automatic=automatic)


async def _approve_deposit_appeal(db: AsyncSession, appeal: Appeal, *, actor_id=None, automatic: bool = False) -> object | None:
    if appeal.operation_type != 'deposit':
        raise ValueError('only deposit appeals can be approved from cabinet')
    dep, appeal = await _lock_deposit_then_appeal(db, appeal)
    if appeal.status not in {
        AppealStatus.opened.value,
        AppealStatus.in_review.value,
    }:
        raise ValueError('appeal has already been resolved')

    meta = metadata(appeal)
    if automatic and (
        meta.get('trader_decision')
        or appeal_deadline(appeal) > utcnow()
    ):
        raise ValueError('appeal is not eligible for automatic approval')
    if meta.get('finance_applied') is True:
        raise ValueError('appeal finance has already been applied')
    previous = meta.get('previous_deposit_status')
    unpaid = {DepositStatus.created.value, DepositStatus.pending.value, DepositStatus.failed.value,
              DepositStatus.expired.value, DepositStatus.cancelled.value}
    if previous not in unpaid | {DepositStatus.paid.value}:
        raise ValueError('appeal previous operation state is missing; manual review required')
    if dep.status not in unpaid | {DepositStatus.paid.value, DepositStatus.appeal_opened.value}:
        raise ValueError('appeal operation state is unsupported; manual review required')
    if previous == DepositStatus.paid.value and dep.status not in {DepositStatus.paid.value, DepositStatus.appeal_opened.value}:
        raise ValueError('paid appeal operation state changed; manual review required')
    # A known prior paid outcome, or a payment completed while this dispute was
    # open, is a claim decision only. Never reopen a paid Rolling allocation.
    already_paid = previous == DepositStatus.paid.value or dep.status == DepositStatus.paid.value
    settlement = None
    if not already_paid:
        await settle_trader_for_approved_appeal(db, dep, meta.get('trader_id'))
        await reopen_released_allocation(db, dep, reason='appeal approved', idempotency_suffix=str(appeal.id))
        dep.status = DepositStatus.paid.value
        settlement = await settle_deposit_credit(db, merchant_id=dep.merchant_id, method=dep.method,
            amount=dep.amount, operation_id=dep.id, description='appeal approved deposit confirmation')
        await check_payment_result_for_risk(db, dep)
    dep.status = DepositStatus.paid.value
    appeal.status = AppealStatus.approved.value
    appeal.decision = 'approved_auto' if automatic else 'approved'
    set_metadata(
        appeal,
        finance_applied=not already_paid,
        financial_resolution='already_paid' if already_paid else 'new_payment',
        approved_at=utcnow().isoformat(),
        approved_by=str(actor_id) if actor_id else None,
        automatic_approval=automatic,
        settlement=settlement,
    )
    db.add(AppealMessage(
        appeal_id=appeal.id,
        author_id=actor_id,
        message='Апелляция автоматически одобрена по истечению таймера.' if automatic else 'Апелляция одобрена.',
    ))
    await db.flush()
    await audit(db, 'deposit_appeal_approved', 'appeal', actor_id, appeal.id, details={
        'deposit_id':str(dep.id), 'previous_deposit_status':previous,
        'financial_resolution':'already_paid' if already_paid else 'new_payment',
        'finance_applied':not already_paid, 'automatic':automatic,
    })
    if already_paid:
        return None  # No false new-credit event or duplicate partner callback.
    await db.refresh(dep)
    await sync_payment_from_deposit(db, dep)  # Durable callbacks use the existing due-delivery job.
    return await queue_webhook(db, dep.merchant_id, 'deposit.paid', deposit_payload(dep, appeal))


async def reject_deposit_appeal(db: AsyncSession, appeal: Appeal, *, actor_id, decision: str = 'rejected') -> object | None:
    if appeal.operation_type != 'deposit':
        raise ValueError('only deposit appeals can be rejected from cabinet')
    dep, appeal = await _lock_deposit_then_appeal(db, appeal)
    if appeal.status not in {
        AppealStatus.opened.value,
        AppealStatus.in_review.value,
    }:
        raise ValueError('appeal has already been resolved')
    meta = metadata(appeal)
    previous_status = meta.get('previous_deposit_status') or DepositStatus.failed.value
    if dep.status == DepositStatus.appeal_opened.value:
        dep.status = previous_status if previous_status != DepositStatus.appeal_opened.value else DepositStatus.failed.value
        await check_payment_result_for_risk(db, dep)
    appeal.status = AppealStatus.rejected.value
    appeal.decision = decision[:64]
    set_metadata(appeal, rejected_at=utcnow().isoformat(), rejected_by=str(actor_id), finance_applied=False)
    db.add(AppealMessage(appeal_id=appeal.id, author_id=actor_id, message='Апелляция отклонена.'))
    await db.flush()
    await db.refresh(dep)
    return await queue_webhook(db, dep.merchant_id, f'deposit.{dep.status}', deposit_payload(dep, appeal))


async def approve_expired_appeals(db: AsyncSession) -> list:
    rows = (await db.execute(
        select(Appeal)
        .where(Appeal.status == AppealStatus.opened.value)
        .order_by(Appeal.created_at.asc())
        .limit(200)
    )).scalars().all()
    events = []
    now = utcnow()
    for appeal in rows:
        meta = metadata(appeal)
        if meta.get('trader_decision'):
            continue
        if appeal_deadline(appeal) > now:
            continue
        try:
            event = await approve_deposit_appeal(db, appeal, automatic=True)
        except ValueError as exc:
            set_metadata(appeal, auto_approve_error=str(exc), auto_approve_failed_at=now.isoformat())
            continue
        if event:
            events.append(event)
    return events


async def resolve_operation_appeal(db: AsyncSession, appeal: Appeal, *, status: str, actor_id, decision: str):
    """One finance-aware boundary. Operation is always locked before Appeal.

    Reject/close/return dismisses the claim and restores its prior operation
    state; it is not an instruction to cancel the payment or release its hold.
    """
    allowed = {AppealStatus.approved.value, AppealStatus.rejected.value,
               AppealStatus.returned_to_processing.value, AppealStatus.closed.value}
    if status not in allowed:
        raise ValueError('invalid final status')
    if appeal.operation_type == 'deposit':
        if status == AppealStatus.approved.value:
            event = await approve_deposit_appeal(db, appeal, actor_id=actor_id)
        else:
            # API-created appeals now record the prior state. Never guess a
            # finance-changing result for an older ambiguous active dispute.
            dep = await db.get(Deposit, appeal.operation_id)
            if dep and dep.status == DepositStatus.appeal_opened.value and metadata(appeal).get('previous_deposit_status') not in {DepositStatus.created.value, DepositStatus.pending.value, DepositStatus.paid.value, DepositStatus.failed.value, DepositStatus.expired.value, DepositStatus.cancelled.value}:
                raise ValueError('appeal previous operation state is missing; manual review required')
            event = await reject_deposit_appeal(db, appeal, actor_id=actor_id, decision=decision or status)
        appeal.status = status
        appeal.decision = (decision or status)[:64]
        return event
    if appeal.operation_type != 'payout':
        raise ValueError('unsupported appeal operation type')
    payout = (await db.execute(select(Payout).where(Payout.id == appeal.operation_id)
              .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    locked = (await db.execute(select(Appeal).where(Appeal.id == appeal.id)
              .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if not payout or not locked or locked.operation_id != payout.id or locked.operation_type != 'payout':
        raise ValueError('appeal target not found')
    appeal = locked
    if appeal.status not in APPEAL_ACTIVE_STATUSES:
        raise ValueError('appeal has already been resolved')
    previous = metadata(appeal).get('previous_payout_status')
    if payout.status == PayoutStatus.appeal_opened.value:
        if previous not in {PayoutStatus.pending.value, PayoutStatus.processing.value}:
            raise ValueError('appeal previous operation state is missing; manual review required')
        payout.status = previous
    event = None
    if status == AppealStatus.approved.value:
        from app.services.payouts import complete_payout
        from app.services.webhook_payloads import build_payout_webhook_payload
        if payout.status != PayoutStatus.completed.value:
            # Never recreate a released hold or debit available funds to turn
            # an already failed/cancelled payout into a completed one.
            await complete_payout(db, payout)
            await db.flush()
            await db.refresh(payout, attribute_names=['updated_at'])
            event = await queue_webhook(db, payout.merchant_id, 'payout.completed', build_payout_webhook_payload(payout))
    appeal.status = status
    appeal.decision = (decision or status)[:64]
    set_metadata(appeal, finance_applied=status == AppealStatus.approved.value,
                 resolved_by=str(actor_id), resolved_at=utcnow().isoformat())
    db.add(AppealMessage(appeal_id=appeal.id, author_id=actor_id,
                        message='Обращение рассмотрено.'))
    return event
