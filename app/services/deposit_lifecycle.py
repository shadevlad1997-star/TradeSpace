from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import DepositStatus, Role
from app.core.metrics import metrics_registry
from app.models import (
    AggregatorPayment,
    Appeal,
    AuditLog,
    Balance,
    Deposit,
    LedgerEntry,
    MerchantRollingAccount,
    MerchantRollingAllocation,
    MerchantRollingLedgerEntry,
    MerchantRollingTransfer,
    MerchantRollingTransferConsumption,
    OperationFeeSnapshot,
    PlatformLedgerEntry,
    Requisite,
    TeamLeadAccrual,
    TeamLeadBalance,
    TeamLeadLedgerEntry,
    TraderLedgerEntry,
    User,
    WebhookEvent,
)
from app.services.aggregators import sync_payment_from_deposit
from app.services.antiscam import check_payment_result_for_risk
from app.services.audit import audit
from app.services.requisites import release_trader_deposit_hold
from app.services.rolling import release_pending_allocation
from app.services.deposit_ttl import (
    aware,
    deposit_deadline,
    deposit_is_expired,
    deposit_remaining_seconds,
    new_deposit_expires_at,
    utcnow,
)
from app.services.webhook_payloads import build_deposit_webhook_payload
from app.services.webhooks import queue_webhook


logger = logging.getLogger('app.finance')


ACTIVE_DEPOSIT_STATUSES = {
    DepositStatus.created.value,
    DepositStatus.pending.value,
    DepositStatus.appeal_opened.value,
}
TTL_EXPIRABLE_DEPOSIT_STATUSES = {
    DepositStatus.created.value,
    DepositStatus.pending.value,
}
UNSUCCESSFUL_DEPOSIT_STATUSES = {
    DepositStatus.failed.value,
    DepositStatus.expired.value,
    DepositStatus.cancelled.value,
}


@dataclass(frozen=True)
class DepositFinalizationResult:
    deposit_id: UUID
    changed: bool
    status: str
    reason: str
    webhook_event_id: UUID | None = None
    aggregator_callback_log_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True)
class LegacyPendingRepairEvidence:
    deposit_id: UUID
    merchant_id: UUID
    trader_id: UUID | None
    amount: Decimal
    status: str
    expired: bool
    category: str
    eligible: bool
    blockers: tuple[str, ...]
    trader_balance: Decimal
    trader_hold: Decimal
    trader_available: Decimal
    trader_ledger_count: int
    merchant_ledger_count: int
    platform_ledger_count: int
    rolling_allocation_count: int
    rolling_ledger_count: int
    rolling_consumption_count: int
    teamlead_accrual_count: int
    paid_webhook_count: int
    paid_aggregator_count: int
    paid_audit_count: int
    fee_snapshot_status: str | None


@dataclass(frozen=True)
class LegacyPendingRepairResult:
    evidence: LegacyPendingRepairEvidence
    dry_run: bool
    changed: bool
    status: str
    failure_reason: str
    financial_fingerprint_before: str
    financial_fingerprint_after: str
    webhook_event_id: UUID | None = None
    aggregator_callback_log_ids: tuple[UUID, ...] = ()


class LegacyPendingRepairBlocked(ValueError):
    def __init__(self, evidence: LegacyPendingRepairEvidence):
        self.evidence = evidence
        super().__init__(
            'legacy pending repair blocked: ' + ','.join(evidence.blockers)
        )


def _decimal(value) -> Decimal:
    return Decimal(value or '0')


async def _financial_fingerprint(db: AsyncSession) -> str:
    """Hash financial state only; audit/outbox rows are intentionally excluded."""
    queries = (
        (
            'merchant_balances',
            select(
                Balance.id,
                Balance.merchant_id,
                Balance.currency,
                Balance.available,
                Balance.frozen,
            ).order_by(Balance.id),
        ),
        (
            'trader_balances',
            select(
                User.id,
                User.trader_balance,
                User.trader_hold,
            )
            .where(User.role.in_([Role.operator.value, Role.trader.value]))
            .order_by(User.id),
        ),
        (
            'teamlead_balances',
            select(
                TeamLeadBalance.id,
                TeamLeadBalance.available_rub,
                TeamLeadBalance.frozen_rub,
                TeamLeadBalance.debt_rub,
                TeamLeadBalance.total_earned_rub,
                TeamLeadBalance.total_paid_rub,
            ).order_by(TeamLeadBalance.id),
        ),
        (
            'rolling_accounts',
            select(
                MerchantRollingAccount.id,
                MerchantRollingAccount.principal_usdt,
                MerchantRollingAccount.recovered_usdt,
                MerchantRollingAccount.outstanding_usdt,
                MerchantRollingAccount.status,
            ).order_by(MerchantRollingAccount.id),
        ),
        (
            'rolling_transfers',
            select(
                MerchantRollingTransfer.id,
                MerchantRollingTransfer.amount_usdt,
                MerchantRollingTransfer.recovered_usdt,
                MerchantRollingTransfer.remaining_usdt,
                MerchantRollingTransfer.status,
            ).order_by(MerchantRollingTransfer.id),
        ),
        (
            'rolling_allocations',
            select(
                MerchantRollingAllocation.id,
                MerchantRollingAllocation.rolling_applied_usdt,
                MerchantRollingAllocation.rolling_applied_rub,
                MerchantRollingAllocation.settle_credited_rub,
                MerchantRollingAllocation.status,
            ).order_by(MerchantRollingAllocation.id),
        ),
        (
            'merchant_ledger',
            select(
                LedgerEntry.id,
                LedgerEntry.operation_id,
                LedgerEntry.entry_type,
                LedgerEntry.amount,
            ).order_by(LedgerEntry.id),
        ),
        (
            'trader_ledger',
            select(
                TraderLedgerEntry.id,
                TraderLedgerEntry.operation_id,
                TraderLedgerEntry.entry_type,
                TraderLedgerEntry.amount,
                TraderLedgerEntry.balance_after,
                TraderLedgerEntry.hold_after,
            ).order_by(TraderLedgerEntry.id),
        ),
        (
            'platform_ledger',
            select(
                PlatformLedgerEntry.id,
                PlatformLedgerEntry.operation_id,
                PlatformLedgerEntry.entry_type,
                PlatformLedgerEntry.amount,
            ).order_by(PlatformLedgerEntry.id),
        ),
        (
            'teamlead_ledger',
            select(
                TeamLeadLedgerEntry.id,
                TeamLeadLedgerEntry.deposit_id,
                TeamLeadLedgerEntry.entry_type,
                TeamLeadLedgerEntry.amount_rub,
            ).order_by(TeamLeadLedgerEntry.id),
        ),
        (
            'rolling_ledger',
            select(
                MerchantRollingLedgerEntry.id,
                MerchantRollingLedgerEntry.deposit_id,
                MerchantRollingLedgerEntry.entry_type,
                MerchantRollingLedgerEntry.amount_usdt,
                MerchantRollingLedgerEntry.amount_rub,
            ).order_by(MerchantRollingLedgerEntry.id),
        ),
        (
            'rolling_consumptions',
            select(
                MerchantRollingTransferConsumption.id,
                MerchantRollingTransferConsumption.deposit_id,
                MerchantRollingTransferConsumption.entry_type,
                MerchantRollingTransferConsumption.amount_usdt,
                MerchantRollingTransferConsumption.amount_rub,
            ).order_by(MerchantRollingTransferConsumption.id),
        ),
        (
            'fee_snapshots',
            select(
                OperationFeeSnapshot.id,
                OperationFeeSnapshot.deposit_id,
                OperationFeeSnapshot.settlement_status,
                OperationFeeSnapshot.ledger_reference,
            ).order_by(OperationFeeSnapshot.id),
        ),
    )
    digest = hashlib.sha256()
    for label, query in queries:
        digest.update(label.encode('utf-8'))
        for row in (await db.execute(query)).all():
            digest.update(repr(tuple(row)).encode('utf-8'))
    return digest.hexdigest()


async def _legacy_pending_repair_evidence(
    db: AsyncSession,
    deposit: Deposit,
    *,
    now: datetime,
    lock_related: bool,
) -> LegacyPendingRepairEvidence:
    blockers: list[str] = []
    metadata = dict(deposit.metadata_json or {})
    expired = deposit_is_expired(deposit, now=now)
    if deposit.status not in TTL_EXPIRABLE_DEPOSIT_STATUSES:
        blockers.append('deposit_not_active')
    if not expired:
        blockers.append('deposit_not_expired')

    requisite = None
    trader = None
    if deposit.requisites_id:
        query = select(Requisite).where(Requisite.id == deposit.requisites_id)
        if lock_related:
            query = query.with_for_update()
        requisite = (await db.execute(query)).scalar_one_or_none()
    if not requisite or not requisite.trader_id:
        blockers.append('missing_requisite_trader')
    else:
        query = select(User).where(
            User.id == requisite.trader_id,
            User.role.in_([Role.operator.value, Role.trader.value]),
        )
        if lock_related:
            query = query.with_for_update()
        trader = (await db.execute(query)).scalar_one_or_none()
        if trader is None:
            blockers.append('missing_trader')

    trader_id = trader.id if trader else (
        requisite.trader_id if requisite else None
    )
    trader_balance = _decimal(trader.trader_balance if trader else 0)
    trader_hold = _decimal(trader.trader_hold if trader else 0)
    trader_available = trader_balance - trader_hold
    if trader_hold != 0:
        blockers.append('trader_hold_nonzero')

    declared_hold_keys = {
        'trader_hold_amount',
        'trader_settlement_amount',
        'trader_hold_status',
        'trader_hold_released',
        'trader_settled',
    }
    if any(key in metadata for key in declared_hold_keys):
        blockers.append('declared_hold_metadata_present')

    trader_ledger_conditions = [
        TraderLedgerEntry.operation_id == deposit.id,
        TraderLedgerEntry.idempotency_key.contains(str(deposit.id)),
    ]
    if trader_id is not None and deposit.requisites_id is not None:
        trader_ledger_conditions.append(
            (
                (TraderLedgerEntry.trader_id == trader_id)
                & (TraderLedgerEntry.operation_id == deposit.requisites_id)
            )
        )
    trader_ledger_count = len(
        (
            await db.execute(
                select(TraderLedgerEntry.id).where(
                    or_(*trader_ledger_conditions)
                )
            )
        ).scalars().unique().all()
    )
    if trader_ledger_count:
        blockers.append('trader_ledger_evidence')

    merchant_ledger_count = len(
        (
            await db.execute(
                select(LedgerEntry.id).where(
                    LedgerEntry.operation_id == deposit.id
                )
            )
        ).scalars().all()
    )
    if merchant_ledger_count:
        blockers.append('merchant_ledger_evidence')

    platform_ledger_count = len(
        (
            await db.execute(
                select(PlatformLedgerEntry.id).where(
                    PlatformLedgerEntry.operation_id == deposit.id
                )
            )
        ).scalars().all()
    )
    if platform_ledger_count:
        blockers.append('platform_ledger_evidence')

    allocations = (
        (
            await db.execute(
                select(MerchantRollingAllocation).where(
                    MerchantRollingAllocation.deposit_id == deposit.id
                )
            )
        ).scalars().all()
    )
    rolling_allocation_count = len(allocations)
    if rolling_allocation_count:
        blockers.append('rolling_allocation_evidence')

    rolling_ledger_count = len(
        (
            await db.execute(
                select(MerchantRollingLedgerEntry.id).where(
                    MerchantRollingLedgerEntry.deposit_id == deposit.id
                )
            )
        ).scalars().all()
    )
    if rolling_ledger_count:
        blockers.append('rolling_ledger_evidence')

    rolling_consumption_count = len(
        (
            await db.execute(
                select(MerchantRollingTransferConsumption.id).where(
                    MerchantRollingTransferConsumption.deposit_id
                    == deposit.id
                )
            )
        ).scalars().all()
    )
    if rolling_consumption_count:
        blockers.append('rolling_consumption_evidence')

    teamlead_accrual_count = len(
        (
            await db.execute(
                select(TeamLeadAccrual.id).where(
                    TeamLeadAccrual.deposit_id == deposit.id
                )
            )
        ).scalars().all()
    )
    if teamlead_accrual_count:
        blockers.append('teamlead_accrual_evidence')

    paid_webhook_count = len(
        (
            await db.execute(
                select(WebhookEvent.id).where(
                    WebhookEvent.payload['id'].as_string() == str(deposit.id),
                    or_(
                        WebhookEvent.event_type == 'deposit.paid',
                        WebhookEvent.payload['status'].as_string() == 'paid',
                    ),
                )
            )
        ).scalars().all()
    )
    if paid_webhook_count:
        blockers.append('paid_webhook_evidence')

    paid_aggregator_count = len(
        (
            await db.execute(
                select(AggregatorPayment.id).where(
                    AggregatorPayment.platform_payment_id == deposit.id,
                    AggregatorPayment.status == 'paid',
                )
            )
        ).scalars().all()
    )
    if paid_aggregator_count:
        blockers.append('paid_aggregator_evidence')

    paid_audit_count = len(
        (
            await db.execute(
                select(AuditLog.id).where(
                    or_(
                        (
                            (AuditLog.target_id == str(deposit.id))
                            & AuditLog.action.contains('paid')
                        ),
                        (
                            AuditLog.details['deposit_id'].as_string()
                            == str(deposit.id)
                        )
                        & (
                            AuditLog.details['new_status'].as_string()
                            == 'paid'
                        ),
                    )
                )
            )
        ).scalars().all()
    )
    if paid_audit_count:
        blockers.append('paid_audit_evidence')

    approved_appeal_count = len(
        (
            await db.execute(
                select(Appeal.id).where(
                    Appeal.operation_id == deposit.id,
                    or_(
                        Appeal.status == 'approved',
                        Appeal.decision == 'approved',
                    ),
                )
            )
        ).scalars().all()
    )
    if approved_appeal_count:
        blockers.append('approved_appeal_evidence')

    snapshot = (
        await db.execute(
            select(OperationFeeSnapshot).where(
                OperationFeeSnapshot.deposit_id == deposit.id
            )
        )
    ).scalar_one_or_none()
    fee_snapshot_status = snapshot.settlement_status if snapshot else None
    if snapshot:
        fee_total = _decimal(snapshot.executor_fee_amount) + _decimal(
            snapshot.platform_income_amount
        )
        if (
            snapshot.settlement_status != 'pending'
            or snapshot.ledger_reference is not None
            or _decimal(snapshot.merchant_fee_amount) != fee_total
        ):
            blockers.append('settled_or_inconsistent_fee_snapshot')

    unique_blockers = tuple(dict.fromkeys(blockers))
    eligible = not unique_blockers
    return LegacyPendingRepairEvidence(
        deposit_id=deposit.id,
        merchant_id=deposit.merchant_id,
        trader_id=trader_id,
        amount=_decimal(deposit.amount),
        status=deposit.status,
        expired=expired,
        category='A' if eligible else 'B',
        eligible=eligible,
        blockers=unique_blockers,
        trader_balance=trader_balance,
        trader_hold=trader_hold,
        trader_available=trader_available,
        trader_ledger_count=trader_ledger_count,
        merchant_ledger_count=merchant_ledger_count,
        platform_ledger_count=platform_ledger_count,
        rolling_allocation_count=rolling_allocation_count,
        rolling_ledger_count=rolling_ledger_count,
        rolling_consumption_count=rolling_consumption_count,
        teamlead_accrual_count=teamlead_accrual_count,
        paid_webhook_count=paid_webhook_count,
        paid_aggregator_count=paid_aggregator_count,
        paid_audit_count=paid_audit_count,
        fee_snapshot_status=fee_snapshot_status,
    )


async def _finalize_unsuccessful_deposit(
    db: AsyncSession,
    deposit: Deposit,
    *,
    reason: str,
    target_status: str = DepositStatus.failed.value,
    actor_id: UUID | None = None,
    actor_role: str = 'system',
    actor_ip: str | None = None,
    request_id: str | None = None,
    audit_action: str = 'deposit_finalized_unsuccessfully',
    release_trader_hold: bool,
    audit_details: dict | None = None,
) -> DepositFinalizationResult:
    """Finalize a locked deposit once and persist all release/outbox rows."""
    if target_status not in UNSUCCESSFUL_DEPOSIT_STATUSES:
        raise ValueError('target status must be an unsuccessful final deposit status')
    reason = (reason or '').strip()
    if not reason:
        raise ValueError('deposit finalization reason is required')
    old_status = deposit.status
    if old_status not in ACTIVE_DEPOSIT_STATUSES:
        return DepositFinalizationResult(
            deposit_id=deposit.id,
            changed=False,
            status=old_status,
            reason=reason,
        )

    deposit.status = target_status
    finalized_at = utcnow()
    metadata = dict(deposit.metadata_json or {})
    metadata.update({
        'failure_reason': reason,
        'finalized_at': finalized_at.isoformat(),
        'finalized_by_role': actor_role,
    })
    if request_id:
        metadata['finalization_request_id'] = request_id
    deposit.metadata_json = metadata

    # Global lock order: Deposit -> Requisite/Trader -> Rolling account/allocation.
    if release_trader_hold:
        await release_trader_deposit_hold(db, deposit)
    await release_pending_allocation(db, deposit, reason=reason)
    await check_payment_result_for_risk(db, deposit)
    # PostgreSQL server_onupdate expires updated_at after autoflush. Refresh it
    # explicitly so the synchronous webhook snapshot builder never triggers
    # implicit async I/O (MissingGreenlet).
    await db.flush()
    await db.refresh(deposit, attribute_names=['updated_at'])
    event_type = (
        'deposit.cancelled'
        if target_status == DepositStatus.cancelled.value
        else 'deposit.failed'
    )
    event = await queue_webhook(
        db,
        deposit.merchant_id,
        event_type,
        build_deposit_webhook_payload(
            deposit,
            event_type,
            {'failure_reason': reason},
        ),
    )
    aggregator_logs = await sync_payment_from_deposit(db, deposit)
    details = {
        'actor_role': actor_role,
        'deposit_id': str(deposit.id),
        'reason': reason,
        'old_status': old_status,
        'new_status': target_status,
        'timestamp': finalized_at.isoformat(),
        'request_id': request_id,
    }
    if audit_details:
        details.update(audit_details)
    await audit(
        db,
        audit_action,
        'deposit',
        actor_id,
        deposit.id,
        actor_ip,
        details,
    )
    return DepositFinalizationResult(
        deposit_id=deposit.id,
        changed=True,
        status=deposit.status,
        reason=reason,
        webhook_event_id=event.id,
        aggregator_callback_log_ids=tuple(row.id for row in aggregator_logs),
    )


async def finalize_unsuccessful_deposit(
    db: AsyncSession,
    deposit: Deposit,
    *,
    reason: str,
    target_status: str = DepositStatus.failed.value,
    actor_id: UUID | None = None,
    actor_role: str = 'system',
    actor_ip: str | None = None,
    request_id: str | None = None,
    audit_action: str = 'deposit_finalized_unsuccessfully',
) -> DepositFinalizationResult:
    """Ordinary lifecycle: releasing a real trader hold remains mandatory."""
    return await _finalize_unsuccessful_deposit(
        db,
        deposit,
        reason=reason,
        target_status=target_status,
        actor_id=actor_id,
        actor_role=actor_role,
        actor_ip=actor_ip,
        request_id=request_id,
        audit_action=audit_action,
        release_trader_hold=True,
    )


async def repair_legacy_pending_without_hold(
    db: AsyncSession,
    *,
    deposit_id: UUID,
    actor_id: UUID,
    reason: str,
    dry_run: bool = True,
    now: datetime | None = None,
) -> LegacyPendingRepairResult:
    """Finalize one proven legacy orphan without fabricating hold movements."""
    reason = (reason or '').strip()
    if not reason:
        raise ValueError('legacy repair reason is required')
    current = aware(now) if now else utcnow()
    query = select(Deposit).where(Deposit.id == deposit_id)
    if not dry_run:
        query = query.with_for_update()
    deposit = (await db.execute(query)).scalar_one_or_none()
    if deposit is None:
        raise ValueError('deposit not found')
    actor = (
        await db.execute(
            select(User).where(
                User.id == actor_id,
                User.role == Role.superadmin.value,
                User.is_active.is_(True),
                User.is_archived.is_(False),
            )
        )
    ).scalar_one_or_none()
    if actor is None:
        raise ValueError('active superadmin actor is required')

    metadata = dict(deposit.metadata_json or {})
    if (
        deposit.status in UNSUCCESSFUL_DEPOSIT_STATUSES
        and metadata.get('failure_reason') == 'legacy_missing_hold'
    ):
        fingerprint = await _financial_fingerprint(db)
        evidence = LegacyPendingRepairEvidence(
            deposit_id=deposit.id,
            merchant_id=deposit.merchant_id,
            trader_id=(
                UUID(metadata['legacy_repair_trader_id'])
                if metadata.get('legacy_repair_trader_id')
                else None
            ),
            amount=_decimal(deposit.amount),
            status=deposit.status,
            expired=True,
            category='A',
            eligible=True,
            blockers=(),
            trader_balance=Decimal('0'),
            trader_hold=Decimal('0'),
            trader_available=Decimal('0'),
            trader_ledger_count=0,
            merchant_ledger_count=0,
            platform_ledger_count=0,
            rolling_allocation_count=0,
            rolling_ledger_count=0,
            rolling_consumption_count=0,
            teamlead_accrual_count=0,
            paid_webhook_count=0,
            paid_aggregator_count=0,
            paid_audit_count=0,
            fee_snapshot_status=None,
        )
        return LegacyPendingRepairResult(
            evidence=evidence,
            dry_run=dry_run,
            changed=False,
            status=deposit.status,
            failure_reason='legacy_missing_hold',
            financial_fingerprint_before=fingerprint,
            financial_fingerprint_after=fingerprint,
        )

    evidence = await _legacy_pending_repair_evidence(
        db,
        deposit,
        now=current,
        lock_related=not dry_run,
    )
    fingerprint_before = await _financial_fingerprint(db)
    if not evidence.eligible:
        if not dry_run:
            raise LegacyPendingRepairBlocked(evidence)
        return LegacyPendingRepairResult(
            evidence=evidence,
            dry_run=True,
            changed=False,
            status=deposit.status,
            failure_reason='',
            financial_fingerprint_before=fingerprint_before,
            financial_fingerprint_after=fingerprint_before,
        )
    if dry_run:
        return LegacyPendingRepairResult(
            evidence=evidence,
            dry_run=True,
            changed=False,
            status=deposit.status,
            failure_reason='legacy_missing_hold',
            financial_fingerprint_before=fingerprint_before,
            financial_fingerprint_after=fingerprint_before,
        )

    metadata.update(
        {
            'legacy_repair_reason': reason,
            'legacy_repair_actor_id': str(actor_id),
            'legacy_repair_trader_id': (
                str(evidence.trader_id) if evidence.trader_id else None
            ),
            'legacy_repair_category': 'A',
        }
    )
    deposit.metadata_json = metadata
    result = await _finalize_unsuccessful_deposit(
        db,
        deposit,
        reason='legacy_missing_hold',
        target_status=DepositStatus.failed.value,
        actor_id=actor_id,
        actor_role=Role.superadmin.value,
        audit_action='legacy_pending_without_hold_repaired',
        release_trader_hold=False,
        audit_details={
            'repair_reason': reason,
            'repair_category': 'A',
            'financial_fingerprint_before': fingerprint_before,
        },
    )
    fingerprint_after = await _financial_fingerprint(db)
    if fingerprint_after != fingerprint_before:
        raise RuntimeError('legacy repair changed financial fingerprint')
    return LegacyPendingRepairResult(
        evidence=evidence,
        dry_run=False,
        changed=result.changed,
        status=result.status,
        failure_reason='legacy_missing_hold',
        financial_fingerprint_before=fingerprint_before,
        financial_fingerprint_after=fingerprint_after,
        webhook_event_id=result.webhook_event_id,
        aggregator_callback_log_ids=result.aggregator_callback_log_ids,
    )


async def expire_due_deposits(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = 200,
) -> list[DepositFinalizationResult]:
    current = aware(now) if now else utcnow()
    rows = (await db.execute(
        select(Deposit)
        .where(
            Deposit.status.in_(TTL_EXPIRABLE_DEPOSIT_STATUSES),
            Deposit.expires_at <= current,
        )
        .order_by(Deposit.expires_at.asc(), Deposit.id.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )).scalars().all()
    results = []
    for deposit in rows:
        deposit_id = deposit.id
        try:
            async with db.begin_nested():
                result = await finalize_unsuccessful_deposit(
                    db,
                    deposit,
                    reason='trader_timeout',
                    target_status=DepositStatus.failed.value,
                    actor_role='system',
                    audit_action='deposit_expired_by_ttl',
                )
        except ValueError as exc:
            metrics_registry.increment(
                'processing_platform_deposit_expiry_errors_total',
                {'reason': type(exc).__name__},
            )
            logger.error(
                'deposit_expiry_financial_invariant_failed',
                extra={
                    'operation_id': str(deposit_id),
                    'reason': str(exc)[:500],
                },
                exc_info=True,
            )
            continue
        if result.changed:
            results.append(result)
    return results
