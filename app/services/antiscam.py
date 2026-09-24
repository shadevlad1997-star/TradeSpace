from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import DepositStatus, RiskAutoAction, RiskSeverity, TrafficStatus, Role
from app.models import (
    AntiscamGlobalSettings,
    AuditLog,
    Deposit,
    Requisite,
    RequisiteAntiscamSettings,
    RiskDecisionRecord,
    RiskEvent,
    TraderAntiscamSettings,
    User,
)
from app.services.audit import audit


ACTIVE_DEPOSIT_STATUSES = {
    DepositStatus.created.value,
    DepositStatus.pending.value,
    DepositStatus.appeal_opened.value,
}
SUCCESS_DEPOSIT_STATUSES = {DepositStatus.paid.value}
FAILED_DEPOSIT_STATUSES = {
    DepositStatus.failed.value,
    DepositStatus.expired.value,
    DepositStatus.cancelled.value,
}
DISPUTED_DEPOSIT_STATUSES = {DepositStatus.appeal_opened.value}
BLOCKING_TRAFFIC_STATUSES = {
    TrafficStatus.auto_paused.value,
    TrafficStatus.manual_paused.value,
    TrafficStatus.under_review.value,
    TrafficStatus.blocked.value,
}
ROUTABLE_TRAFFIC_STATUSES = {
    TrafficStatus.active.value,
    TrafficStatus.reinstated_limited.value,
    TrafficStatus.reinstated_full.value,
}


@dataclass
class RiskStats:
    total_payments: int = 0
    successful_payments: int = 0
    failed_payments: int = 0
    expired_payments: int = 0
    rejected_payments: int = 0
    disputed_payments: int = 0
    active_payments: int = 0
    conversion_percent: Decimal = Decimal("0.00")
    average_confirmation_time_minutes: Decimal = Decimal("0.00")
    amount_at_risk: Decimal = Decimal("0.00")
    failed_in_row: int = 0


@dataclass
class EffectiveTraderAntiscamSettings:
    antiscam_enabled: bool
    failed_payments_in_row_limit: int
    min_conversion_percent: Decimal
    conversion_check_window_minutes: int
    conversion_drop_percent_limit: Decimal
    max_confirmation_delay_minutes: int
    max_active_payments_when_risky: int
    allow_high_amount_traffic: bool
    risk_level: str
    auto_disable_requisites_enabled: bool
    auto_disable_trader_enabled: bool
    freeze_withdrawals_on_auto_pause: bool


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def money(value: Decimal | int | str | None) -> Decimal:
    return Decimal(value or "0.00").quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def percent(value: Decimal | int | str | None) -> Decimal:
    raw = Decimal(value or "0.00")
    if raw < 0:
        raw = Decimal("0.00")
    if raw > 100:
        raw = Decimal("100.00")
    return raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def clamp_score(value: int) -> int:
    return max(0, min(100, int(value)))


def severity_for_score(score: int) -> str:
    if score >= 86:
        return RiskSeverity.critical.value
    if score >= 71:
        return RiskSeverity.high.value
    if score >= 51:
        return RiskSeverity.medium.value
    return RiskSeverity.warning.value


async def get_global_settings(db: AsyncSession, *, for_update: bool = False) -> AntiscamGlobalSettings:
    stmt = select(AntiscamGlobalSettings).where(AntiscamGlobalSettings.id == 1)
    if for_update:
        stmt = stmt.with_for_update()
    settings = (await db.execute(stmt)).scalar_one_or_none()
    if settings:
        return settings
    settings = AntiscamGlobalSettings(id=1)
    db.add(settings)
    await db.flush()
    return settings


async def get_or_create_trader_settings(db: AsyncSession, trader_id: UUID) -> TraderAntiscamSettings:
    settings = (await db.execute(
        select(TraderAntiscamSettings).where(TraderAntiscamSettings.trader_id == trader_id)
    )).scalar_one_or_none()
    if settings:
        return settings
    settings = TraderAntiscamSettings(trader_id=trader_id, risk_level="strict", allow_high_amount_traffic=False)
    db.add(settings)
    await db.flush()
    return settings


async def get_or_create_requisite_settings(db: AsyncSession, requisite_id: UUID) -> RequisiteAntiscamSettings:
    settings = (await db.execute(
        select(RequisiteAntiscamSettings).where(RequisiteAntiscamSettings.requisite_id == requisite_id)
    )).scalar_one_or_none()
    if settings:
        return settings
    settings = RequisiteAntiscamSettings(requisite_id=requisite_id)
    db.add(settings)
    await db.flush()
    return settings


async def effective_trader_settings(db: AsyncSession, trader: User) -> EffectiveTraderAntiscamSettings:
    global_settings = await get_global_settings(db)
    trader_settings = await get_or_create_trader_settings(db, trader.id)
    if trader_settings.use_global_antiscam_settings:
        return EffectiveTraderAntiscamSettings(
            antiscam_enabled=bool(global_settings.antiscam_enabled),
            failed_payments_in_row_limit=int(global_settings.failed_payments_in_row_limit_trader or 10),
            min_conversion_percent=percent(global_settings.min_trader_conversion_percent),
            conversion_check_window_minutes=int(global_settings.conversion_check_window_minutes or 60),
            conversion_drop_percent_limit=percent(global_settings.conversion_drop_percent_limit),
            max_confirmation_delay_minutes=int(global_settings.max_confirmation_delay_minutes or 10),
            max_active_payments_when_risky=int(global_settings.limited_reinstate_max_active_payments or 3),
            allow_high_amount_traffic=True,
            risk_level=trader_settings.risk_level or "strict",
            auto_disable_requisites_enabled=True,
            auto_disable_trader_enabled=True,
            freeze_withdrawals_on_auto_pause=bool(global_settings.freeze_withdrawals_on_trader_auto_pause),
        )
    return EffectiveTraderAntiscamSettings(
        antiscam_enabled=bool(trader_settings.antiscam_enabled),
        failed_payments_in_row_limit=int(trader_settings.failed_payments_in_row_limit or 10),
        min_conversion_percent=percent(trader_settings.min_conversion_percent),
        conversion_check_window_minutes=int(trader_settings.conversion_check_window_minutes or 60),
        conversion_drop_percent_limit=percent(trader_settings.conversion_drop_percent_limit),
        max_confirmation_delay_minutes=int(trader_settings.max_confirmation_delay_minutes or 10),
        max_active_payments_when_risky=int(trader_settings.max_active_payments_when_risky or 3),
        allow_high_amount_traffic=bool(trader_settings.allow_high_amount_traffic),
        risk_level=trader_settings.risk_level or "strict",
        auto_disable_requisites_enabled=bool(trader_settings.auto_disable_requisites_enabled),
        auto_disable_trader_enabled=bool(trader_settings.auto_disable_trader_enabled),
        freeze_withdrawals_on_auto_pause=bool(trader_settings.freeze_withdrawals_on_auto_pause),
    )


async def _requisite_window_stats(db: AsyncSession, req: Requisite, window_minutes: int) -> RiskStats:
    now = utcnow()
    cutoff = now - timedelta(minutes=max(1, int(window_minutes or 60)))
    rows = (await db.execute(
        select(Deposit)
        .where(Deposit.requisites_id == req.id, Deposit.created_at >= cutoff)
        .order_by(Deposit.created_at.asc())
    )).scalars().all()
    return _stats_from_deposits(rows, getattr(req, "failed_in_row", 0) or 0)


async def _trader_window_stats(db: AsyncSession, trader: User, window_minutes: int) -> RiskStats:
    req_ids = (await db.execute(select(Requisite.id).where(Requisite.trader_id == trader.id))).scalars().all()
    if not req_ids:
        return RiskStats()
    cutoff = utcnow() - timedelta(minutes=max(1, int(window_minutes or 60)))
    rows = (await db.execute(
        select(Deposit)
        .where(Deposit.requisites_id.in_(req_ids), Deposit.created_at >= cutoff)
        .order_by(Deposit.created_at.asc())
    )).scalars().all()
    return _stats_from_deposits(rows, await _failed_in_row_for_trader(db, trader))


def _stats_from_deposits(rows: list[Deposit], failed_in_row: int) -> RiskStats:
    success = [row for row in rows if row.status in SUCCESS_DEPOSIT_STATUSES]
    failed = [row for row in rows if row.status in FAILED_DEPOSIT_STATUSES]
    expired = [row for row in rows if row.status == DepositStatus.expired.value]
    rejected = [row for row in rows if row.status in {DepositStatus.failed.value, DepositStatus.cancelled.value}]
    disputed = [row for row in rows if row.status in DISPUTED_DEPOSIT_STATUSES]
    active = [row for row in rows if row.status in ACTIVE_DEPOSIT_STATUSES]
    total_final = len(success) + len(failed)
    conversion = Decimal("0.00")
    if total_final:
        conversion = (Decimal(len(success)) * Decimal("100") / Decimal(total_final)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    confirmation_minutes: list[Decimal] = []
    for row in success:
        created = aware(row.created_at)
        updated = aware(row.updated_at)
        if created and updated and updated >= created:
            confirmation_minutes.append(Decimal((updated - created).total_seconds()) / Decimal("60"))
    average_confirmation = Decimal("0.00")
    if confirmation_minutes:
        average_confirmation = (sum(confirmation_minutes, Decimal("0.00")) / Decimal(len(confirmation_minutes))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return RiskStats(
        total_payments=len(rows),
        successful_payments=len(success),
        failed_payments=len(failed),
        expired_payments=len(expired),
        rejected_payments=len(rejected),
        disputed_payments=len(disputed),
        active_payments=len(active),
        conversion_percent=conversion,
        average_confirmation_time_minutes=average_confirmation,
        amount_at_risk=sum((money(row.amount) for row in active), Decimal("0.00")),
        failed_in_row=failed_in_row,
    )


async def _failed_in_row_for_trader(db: AsyncSession, trader: User) -> int:
    req_ids = (await db.execute(select(Requisite.id).where(Requisite.trader_id == trader.id))).scalars().all()
    if not req_ids:
        return 0
    rows = (await db.execute(
        select(Deposit.status)
        .where(Deposit.requisites_id.in_(req_ids), Deposit.status.in_(list(SUCCESS_DEPOSIT_STATUSES | FAILED_DEPOSIT_STATUSES)))
        .order_by(Deposit.updated_at.desc(), Deposit.created_at.desc())
        .limit(100)
    )).scalars().all()
    count = 0
    for status in rows:
        if status in SUCCESS_DEPOSIT_STATUSES:
            break
        if status in FAILED_DEPOSIT_STATUSES:
            count += 1
    return count


def _score_from_stats(
    *,
    stats: RiskStats,
    previous_score: int,
    failed_limit: int,
    min_conversion_percent: Decimal,
    min_payments_for_conversion_check: int,
    max_confirmation_delay_minutes: int,
    high_amount: bool,
    repeated_after_reinstate: bool,
    baseline_conversion_percent: Decimal | None = None,
    conversion_drop_percent_limit: Decimal = Decimal("40"),
    merchant_complaints_limit: int = 0,
) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    if stats.failed_in_row >= failed_limit:
        score += 20
        reasons.append("failed_payments_in_row")
    if stats.total_payments >= min_payments_for_conversion_check and stats.conversion_percent < min_conversion_percent:
        score += 20
        reasons.append("low_conversion")
    baseline_conversion = percent(baseline_conversion_percent) if baseline_conversion_percent is not None else Decimal("0.00")
    drop_limit = percent(conversion_drop_percent_limit)
    if stats.total_payments >= min_payments_for_conversion_check and baseline_conversion >= min_conversion_percent and drop_limit > 0:
        allowed_floor = (baseline_conversion * (Decimal("100.00") - drop_limit) / Decimal("100.00")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if stats.conversion_percent < allowed_floor:
            score += 25
            reasons.append("conversion_drop")
    if stats.average_confirmation_time_minutes > Decimal(max_confirmation_delay_minutes or 0):
        score += 15
        reasons.append("confirmation_delay")
    if merchant_complaints_limit and stats.disputed_payments >= merchant_complaints_limit:
        score += 20
        reasons.append("merchant_complaints")
    if high_amount:
        score += 30
        reasons.append("high_amount_anomaly")
    if stats.expired_payments >= max(3, failed_limit // 2):
        score += 10
        reasons.append("too_many_expired")
    if repeated_after_reinstate and score > 0:
        score += 25
        reasons.append("repeated_after_reinstate")
    if previous_score > score and score > 0:
        score = max(score, previous_score - 5)
    return clamp_score(score), reasons


async def _record_event(
    db: AsyncSession,
    *,
    target_type: str,
    target_id: UUID,
    trader_id: UUID | None,
    requisite_id: UUID | None,
    reason: str,
    old_status: str | None,
    new_status: str | None,
    before_score: int,
    after_score: int,
    stats: RiskStats,
    action: str,
    deposit: Deposit | None = None,
    conversion_before: Decimal | None = None,
    window_minutes: int | None = None,
) -> RiskEvent:
    event = RiskEvent(
        merchant_id=deposit.merchant_id if deposit else None,
        operation_type="antiscam",
        operation_id=deposit.id if deposit else None,
        score=after_score,
        decision="review" if action == RiskAutoAction.none.value else "deny",
        reason=reason,
        details={
            "target_type": target_type,
            "target_id": str(target_id),
            "deposit_id": str(deposit.id) if deposit else None,
        },
        source="antiscam",
        target_type=target_type,
        target_id=target_id,
        trader_id=trader_id,
        requisite_id=requisite_id,
        severity=severity_for_score(after_score),
        old_status=old_status,
        new_status=new_status,
        risk_score_before=before_score,
        risk_score_after=after_score,
        payments_count_in_window=stats.total_payments,
        successful_count_in_window=stats.successful_payments,
        failed_count_in_window=stats.failed_payments,
        conversion_before=conversion_before,
        conversion_after=stats.conversion_percent,
        window_minutes=window_minutes,
        amount_at_risk=stats.amount_at_risk,
        auto_action_taken=action,
    )
    db.add(event)
    await db.flush()
    return event


async def auto_pause_requisite(
    db: AsyncSession,
    *,
    req: Requisite,
    trader: User | None,
    reason: str,
    score: int,
    stats: RiskStats,
    deposit: Deposit | None = None,
    conversion_before: Decimal | None = None,
    window_minutes: int | None = None,
) -> RiskEvent | None:
    old_status = req.traffic_status or TrafficStatus.active.value
    if old_status in {TrafficStatus.manual_paused.value, TrafficStatus.blocked.value}:
        return None
    old_score = int(req.risk_score or 0)
    req.traffic_status = TrafficStatus.auto_paused.value
    req.enabled = False
    req.auto_paused_at = utcnow()
    req.risk_score = score
    event = await _record_event(
        db,
        target_type="requisite",
        target_id=req.id,
        trader_id=req.trader_id,
        requisite_id=req.id,
        reason=reason,
        old_status=old_status,
        new_status=req.traffic_status,
        before_score=old_score,
        after_score=score,
        stats=stats,
        action=RiskAutoAction.pause_requisite.value,
        deposit=deposit,
        conversion_before=conversion_before,
        window_minutes=window_minutes,
    )
    await audit(db, "system_auto_paused_requisite", "requisite", None, req.id, None, {
        "reason": reason,
        "risk_event_id": str(event.id),
        "trader_id": str(trader.id) if trader else None,
    })
    return event


async def auto_pause_trader(
    db: AsyncSession,
    *,
    trader: User,
    reason: str,
    score: int,
    stats: RiskStats,
    freeze_withdrawals: bool,
    deposit: Deposit | None = None,
    conversion_before: Decimal | None = None,
    window_minutes: int | None = None,
) -> RiskEvent | None:
    old_status = trader.trader_traffic_status or TrafficStatus.active.value
    if old_status in {TrafficStatus.manual_paused.value, TrafficStatus.blocked.value}:
        return None
    old_score = int(trader.trader_risk_score or 0)
    trader.trader_traffic_status = TrafficStatus.auto_paused.value
    trader.trader_risk_score = score
    if freeze_withdrawals:
        trader.trader_withdrawals_frozen = True
    event = await _record_event(
        db,
        target_type="trader",
        target_id=trader.id,
        trader_id=trader.id,
        requisite_id=deposit.requisites_id if deposit else None,
        reason=reason,
        old_status=old_status,
        new_status=trader.trader_traffic_status,
        before_score=old_score,
        after_score=score,
        stats=stats,
        action=RiskAutoAction.pause_trader.value if not freeze_withdrawals else RiskAutoAction.freeze_withdrawals.value,
        deposit=deposit,
        conversion_before=conversion_before,
        window_minutes=window_minutes,
    )
    await audit(db, "system_auto_paused_trader", "user", None, trader.id, None, {
        "reason": reason,
        "risk_event_id": str(event.id),
        "withdrawals_frozen": bool(freeze_withdrawals),
    })
    return event


async def check_payment_result_for_risk(db: AsyncSession, deposit: Deposit) -> list[RiskEvent]:
    global_settings = await get_global_settings(db)
    if not global_settings.antiscam_enabled or not deposit.requisites_id:
        return []
    req = (await db.execute(select(Requisite).where(Requisite.id == deposit.requisites_id).with_for_update())).scalar_one_or_none()
    if not req:
        return []
    trader = None
    if req.trader_id:
        trader = (await db.execute(
            select(User).where(User.id == req.trader_id, User.role.in_([Role.operator.value, Role.trader.value])).with_for_update()
        )).scalar_one_or_none()
    now = utcnow()
    req.last_payment_at = now
    if deposit.status in SUCCESS_DEPOSIT_STATUSES:
        req.failed_in_row = 0
    elif deposit.status in FAILED_DEPOSIT_STATUSES:
        req.failed_in_row = int(req.failed_in_row or 0) + 1
    else:
        return []

    req_settings = await get_or_create_requisite_settings(db, req.id)
    if not req_settings.antiscam_enabled:
        return []
    trader_settings = await effective_trader_settings(db, trader) if trader else None
    window = int(req_settings.conversion_check_window_minutes or global_settings.conversion_check_window_minutes or 60)
    req_stats = await _requisite_window_stats(db, req, window)
    req_baseline_stats = await _requisite_window_stats(db, req, 1440)
    high_amount_failed = (
        bool(global_settings.high_amount_extra_risk_enabled)
        and money(deposit.amount) >= money(global_settings.high_amount_threshold)
        and deposit.status in FAILED_DEPOSIT_STATUSES
    )
    req_score, req_reasons = _score_from_stats(
        stats=req_stats,
        previous_score=int(req.risk_score or 0),
        failed_limit=int(req_settings.failed_payments_in_row_limit or global_settings.failed_payments_in_row_limit_requisite or 5),
        min_conversion_percent=percent(req_settings.min_conversion_percent or global_settings.min_requisite_conversion_percent),
        min_payments_for_conversion_check=int(global_settings.min_payments_for_conversion_check or 20),
        max_confirmation_delay_minutes=int(req_settings.max_confirmation_delay_minutes or global_settings.max_confirmation_delay_minutes or 10),
        merchant_complaints_limit=int(global_settings.merchant_complaints_limit_requisite or 3),
        high_amount=high_amount_failed,
        repeated_after_reinstate=(req.traffic_status == TrafficStatus.reinstated_limited.value),
        baseline_conversion_percent=req_baseline_stats.conversion_percent,
        conversion_drop_percent_limit=percent(global_settings.conversion_drop_percent_limit),
    )
    old_req_score = int(req.risk_score or 0)
    req.risk_score = req_score if deposit.status in FAILED_DEPOSIT_STATUSES else max(0, old_req_score - 3)
    events: list[RiskEvent] = []
    auto_disable = bool(global_settings.auto_disable_traffic_enabled)
    if auto_disable and req_reasons:
        failed_limit = int(req_settings.failed_payments_in_row_limit or global_settings.failed_payments_in_row_limit_requisite or 5)
        should_pause_req = req_stats.failed_in_row >= failed_limit or req_score >= 71
        trader_allows_req_pause = True if trader_settings is None else trader_settings.auto_disable_requisites_enabled
        if should_pause_req and trader_allows_req_pause:
            event = await auto_pause_requisite(
                db,
                req=req,
                trader=trader,
                reason=req_reasons[0],
                score=req_score,
                stats=req_stats,
                deposit=deposit,
                conversion_before=req_baseline_stats.conversion_percent,
                window_minutes=window,
            )
            if event:
                events.append(event)

    if trader:
        trader_settings = trader_settings or await effective_trader_settings(db, trader)
        trader_stats = await _trader_window_stats(db, trader, trader_settings.conversion_check_window_minutes)
        trader_baseline_stats = await _trader_window_stats(db, trader, 1440)
        trader_high_amount_failed = high_amount_failed
        trader_score, trader_reasons = _score_from_stats(
            stats=trader_stats,
            previous_score=int(trader.trader_risk_score or 0),
            failed_limit=trader_settings.failed_payments_in_row_limit,
            min_conversion_percent=trader_settings.min_conversion_percent,
            min_payments_for_conversion_check=int(global_settings.min_payments_for_conversion_check or 20),
            max_confirmation_delay_minutes=trader_settings.max_confirmation_delay_minutes,
            merchant_complaints_limit=int(global_settings.merchant_complaints_limit_trader or 5),
            high_amount=trader_high_amount_failed,
            repeated_after_reinstate=(trader.trader_traffic_status == TrafficStatus.reinstated_limited.value),
            baseline_conversion_percent=trader_baseline_stats.conversion_percent,
            conversion_drop_percent_limit=trader_settings.conversion_drop_percent_limit,
        )
        old_trader_score = int(trader.trader_risk_score or 0)
        trader.trader_risk_score = trader_score if deposit.status in FAILED_DEPOSIT_STATUSES else max(0, old_trader_score - 2)
        if auto_disable and trader_reasons and trader_settings.auto_disable_trader_enabled:
            should_pause_trader = trader_stats.failed_in_row >= trader_settings.failed_payments_in_row_limit or trader_score >= 86
            if should_pause_trader:
                event = await auto_pause_trader(
                    db,
                    trader=trader,
                    reason=trader_reasons[0],
                    score=trader_score,
                    stats=trader_stats,
                    freeze_withdrawals=trader_settings.freeze_withdrawals_on_auto_pause,
                    deposit=deposit,
                    conversion_before=trader_baseline_stats.conversion_percent,
                    window_minutes=trader_settings.conversion_check_window_minutes,
                )
                if event:
                    events.append(event)
    return events


async def active_payments_for_trader(db: AsyncSession, trader_id: UUID) -> int:
    req_ids = (await db.execute(select(Requisite.id).where(Requisite.trader_id == trader_id))).scalars().all()
    if not req_ids:
        return 0
    return int((await db.execute(
        select(func.count()).select_from(Deposit).where(
            Deposit.requisites_id.in_(req_ids),
            Deposit.status.in_(list(ACTIVE_DEPOSIT_STATUSES)),
        )
    )).scalar_one())


async def active_payments_for_requisite(db: AsyncSession, requisite_id: UUID) -> int:
    return int((await db.execute(
        select(func.count()).select_from(Deposit).where(
            Deposit.requisites_id == requisite_id,
            Deposit.status.in_(list(ACTIVE_DEPOSIT_STATUSES)),
        )
    )).scalar_one())


async def can_route_payment_to_trader(db: AsyncSession, trader: User | None, amount: Decimal) -> bool:
    if trader is None:
        return False
    status = trader.trader_traffic_status or TrafficStatus.active.value
    if status in BLOCKING_TRAFFIC_STATUSES:
        return False
    if status not in ROUTABLE_TRAFFIC_STATUSES:
        return False
    settings = await effective_trader_settings(db, trader)
    global_settings = await get_global_settings(db)
    amount_value = money(amount)
    if status == TrafficStatus.reinstated_limited.value:
        limited_until = aware(trader.trader_limited_until)
        if limited_until and limited_until < utcnow():
            trader.trader_traffic_status = TrafficStatus.active.value
            trader.trader_limited_until = None
        else:
            max_active = trader.trader_limited_max_active_payments or settings.max_active_payments_when_risky
            if max_active and await active_payments_for_trader(db, trader.id) >= max_active:
                return False
            max_amount = trader.trader_limited_max_amount or global_settings.limited_reinstate_max_amount
            if max_amount and amount_value > money(max_amount):
                return False
    if global_settings.high_amount_extra_risk_enabled and amount_value >= money(global_settings.high_amount_threshold):
        if not bool(trader.trader_allow_high_amount) or not settings.allow_high_amount_traffic:
            return False
    return int(trader.trader_risk_score or 0) < 86


async def can_route_payment_to_requisite(db: AsyncSession, req: Requisite, trader: User | None, amount: Decimal) -> bool:
    status = req.traffic_status or TrafficStatus.active.value
    if status in BLOCKING_TRAFFIC_STATUSES:
        return False
    if status not in ROUTABLE_TRAFFIC_STATUSES:
        return False
    global_settings = await get_global_settings(db)
    req_settings = await get_or_create_requisite_settings(db, req.id)
    if not req_settings.antiscam_enabled:
        return True
    amount_value = money(amount)
    if status == TrafficStatus.reinstated_limited.value:
        limited_until = aware(req.limited_until)
        if limited_until and limited_until < utcnow():
            req.traffic_status = TrafficStatus.active.value
            req.limited_until = None
        else:
            max_active = req.limited_max_active_payments or req_settings.limited_max_active_payments
            if max_active and await active_payments_for_requisite(db, req.id) >= max_active:
                return False
            max_amount = req.limited_max_amount or req_settings.limited_max_amount
            if max_amount and amount_value > money(max_amount):
                return False
    if global_settings.high_amount_extra_risk_enabled and amount_value >= money(global_settings.high_amount_threshold):
        if not bool(req.allow_high_amount) or not bool(req_settings.allow_high_amount_traffic):
            return False
    return int(req.risk_score or 0) < 86 and await can_route_payment_to_trader(db, trader, amount_value)


async def _latest_open_event(db: AsyncSession, target_type: str, target_id: UUID) -> RiskEvent | None:
    return (await db.execute(
        select(RiskEvent)
        .where(
            RiskEvent.source == "antiscam",
            RiskEvent.target_type == target_type,
            RiskEvent.target_id == target_id,
            RiskEvent.resolved_at.is_(None),
        )
        .order_by(RiskEvent.created_at.desc())
        .limit(1)
        .with_for_update()
    )).scalar_one_or_none()


def _lower_reinstated_score(score: int, mode: str) -> int:
    if mode == "full":
        return min(max(0, int(score or 0)), 35)
    return min(max(0, int(score or 0)), 55)


async def reinstate_requisite(
    db: AsyncSession,
    *,
    req: Requisite,
    actor: User,
    decision_reason: str,
    proofs_checked: bool,
    proof_reference: str | None = None,
    reinstate_mode: str = "limited",
) -> RiskDecisionRecord:
    reason = decision_reason.strip()
    if not reason:
        raise ValueError("decision_reason is required")
    global_settings = await get_global_settings(db)
    event = await _latest_open_event(db, "requisite", req.id)
    old_status = req.traffic_status or TrafficStatus.active.value
    old_score = int(req.risk_score or 0)
    if reinstate_mode == "full":
        req.traffic_status = TrafficStatus.reinstated_full.value
        req.enabled = True
        req.allow_high_amount = True
        req.limited_until = None
    elif reinstate_mode in {"limited", "low_amount_only", "no_high_amount"}:
        req.traffic_status = TrafficStatus.reinstated_limited.value
        req.enabled = True
        req.allow_high_amount = False
        req.limited_until = utcnow() + timedelta(minutes=int(global_settings.limited_reinstate_duration_minutes or 120))
        req.limited_max_active_payments = int(global_settings.limited_reinstate_max_active_payments or 3)
        req.limited_max_amount = money(global_settings.limited_reinstate_max_amount)
    elif reinstate_mode == "block":
        req.traffic_status = TrafficStatus.blocked.value
        req.enabled = False
    else:
        req.traffic_status = TrafficStatus.under_review.value
        req.enabled = False
    req.risk_score = _lower_reinstated_score(old_score, "full" if reinstate_mode == "full" else "limited")
    if event:
        event.resolved_at = utcnow()
        event.resolved_by = actor.id
        event.resolution_status = reinstate_mode
        event.resolution_comment = reason
    record = RiskDecisionRecord(
        risk_event_id=event.id if event else None,
        actor_id=actor.id,
        target_type="requisite",
        target_id=req.id,
        trader_id=req.trader_id,
        requisite_id=req.id,
        decision="reinstate" if reinstate_mode != "block" else "block",
        decision_reason=reason,
        proofs_checked=bool(proofs_checked),
        proof_source="Telegram рабочая группа",
        proof_reference=(proof_reference or "").strip()[:500] or None,
        reinstate_mode=reinstate_mode,
        old_status=old_status,
        new_status=req.traffic_status,
        risk_score_before=old_score,
        risk_score_after=req.risk_score,
        limited_until=req.limited_until,
        max_active_payments=req.limited_max_active_payments,
        max_payment_amount=req.limited_max_amount,
        allow_high_amount=req.allow_high_amount,
    )
    db.add(record)
    await audit(db, "superadmin_reinstated_requisite", "requisite", actor.id, req.id, None, {
        "reason": reason,
        "mode": reinstate_mode,
        "risk_event_id": str(event.id) if event else None,
    })
    return record


async def reinstate_trader(
    db: AsyncSession,
    *,
    trader: User,
    actor: User,
    decision_reason: str,
    proofs_checked: bool,
    proof_reference: str | None = None,
    reinstate_mode: str = "limited",
    freeze_withdrawals: bool | None = None,
) -> RiskDecisionRecord:
    reason = decision_reason.strip()
    if not reason:
        raise ValueError("decision_reason is required")
    global_settings = await get_global_settings(db)
    event = await _latest_open_event(db, "trader", trader.id)
    old_status = trader.trader_traffic_status or TrafficStatus.active.value
    old_score = int(trader.trader_risk_score or 0)
    if reinstate_mode == "full":
        trader.trader_traffic_status = TrafficStatus.reinstated_full.value
        trader.trader_allow_high_amount = True
        trader.trader_limited_until = None
    elif reinstate_mode in {"limited", "low_amount_only", "no_high_amount"}:
        trader.trader_traffic_status = TrafficStatus.reinstated_limited.value
        trader.trader_allow_high_amount = False
        trader.trader_limited_until = utcnow() + timedelta(minutes=int(global_settings.limited_reinstate_duration_minutes or 120))
        trader.trader_limited_max_active_payments = int(global_settings.limited_reinstate_max_active_payments or 3)
        trader.trader_limited_max_amount = money(global_settings.limited_reinstate_max_amount)
    elif reinstate_mode == "block":
        trader.trader_traffic_status = TrafficStatus.blocked.value
    else:
        trader.trader_traffic_status = TrafficStatus.under_review.value
    if freeze_withdrawals is not None:
        trader.trader_withdrawals_frozen = bool(freeze_withdrawals)
    trader.trader_risk_score = _lower_reinstated_score(old_score, "full" if reinstate_mode == "full" else "limited")
    if event:
        event.resolved_at = utcnow()
        event.resolved_by = actor.id
        event.resolution_status = reinstate_mode
        event.resolution_comment = reason
    record = RiskDecisionRecord(
        risk_event_id=event.id if event else None,
        actor_id=actor.id,
        target_type="trader",
        target_id=trader.id,
        trader_id=trader.id,
        requisite_id=None,
        decision="reinstate" if reinstate_mode != "block" else "block",
        decision_reason=reason,
        proofs_checked=bool(proofs_checked),
        proof_source="Telegram рабочая группа",
        proof_reference=(proof_reference or "").strip()[:500] or None,
        reinstate_mode=reinstate_mode,
        old_status=old_status,
        new_status=trader.trader_traffic_status,
        risk_score_before=old_score,
        risk_score_after=trader.trader_risk_score,
        limited_until=trader.trader_limited_until,
        max_active_payments=trader.trader_limited_max_active_payments,
        max_payment_amount=trader.trader_limited_max_amount,
        allow_high_amount=trader.trader_allow_high_amount,
    )
    db.add(record)
    await audit(db, "superadmin_reinstated_trader", "user", actor.id, trader.id, None, {
        "reason": reason,
        "mode": reinstate_mode,
        "risk_event_id": str(event.id) if event else None,
        "withdrawals_frozen": bool(trader.trader_withdrawals_frozen),
    })
    return record


async def antiscam_dashboard_rows(db: AsyncSession) -> dict:
    global_settings = await get_global_settings(db)
    traders = (await db.execute(
        select(User)
        .where(User.role.in_([Role.operator.value, Role.trader.value]))
        .order_by(User.trader_risk_score.desc(), User.created_at.desc())
        .limit(300)
    )).scalars().all()
    requisites = (await db.execute(
        select(Requisite)
        .order_by(Requisite.risk_score.desc(), Requisite.created_at.desc())
        .limit(300)
    )).scalars().all()
    events = (await db.execute(
        select(RiskEvent)
        .where(RiskEvent.source == "antiscam")
        .order_by(RiskEvent.created_at.desc())
        .limit(300)
    )).scalars().all()
    active_events = [event for event in events if not event.resolved_at and event.auto_action_taken not in {None, RiskAutoAction.none.value}]
    decisions = (await db.execute(
        select(RiskDecisionRecord).order_by(RiskDecisionRecord.created_at.desc()).limit(300)
    )).scalars().all()
    audits = (await db.execute(
        select(AuditLog)
        .where(
            AuditLog.action.like("%antiscam%")
            | AuditLog.action.like("%pause%")
            | AuditLog.action.like("%reinstated%")
            | AuditLog.action.like("%withdrawals%")
        )
        .order_by(AuditLog.created_at.desc())
        .limit(300)
    )).scalars().all()
    trader_stats = {}
    trader_stats_24h = {}
    requisite_stats = {}
    requisite_stats_24h = {}
    trader_settings = {}
    requisite_settings = {}
    window = int(global_settings.conversion_check_window_minutes or 60)
    for trader in traders:
        trader_stats[str(trader.id)] = await _trader_window_stats(db, trader, window)
        trader_stats_24h[str(trader.id)] = await _trader_window_stats(db, trader, 1440)
        trader_settings[str(trader.id)] = await get_or_create_trader_settings(db, trader.id)
    for req in requisites:
        requisite_stats[str(req.id)] = await _requisite_window_stats(db, req, window)
        requisite_stats_24h[str(req.id)] = await _requisite_window_stats(db, req, 1440)
        requisite_settings[str(req.id)] = await get_or_create_requisite_settings(db, req.id)
    return {
        "global_settings": global_settings,
        "traders": traders,
        "requisites": requisites,
        "events": events,
        "active_events": active_events,
        "decisions": decisions,
        "audits": audits,
        "trader_stats": trader_stats,
        "trader_stats_24h": trader_stats_24h,
        "requisite_stats": requisite_stats,
        "requisite_stats_24h": requisite_stats_24h,
        "trader_settings": trader_settings,
        "requisite_settings": requisite_settings,
    }


async def run_periodic_antiscam_scan(db: AsyncSession, *, limit: int = 200) -> int:
    global_settings = await get_global_settings(db)
    if not global_settings.antiscam_enabled or not global_settings.auto_disable_traffic_enabled:
        return 0
    window = int(global_settings.conversion_check_window_minutes or 60)
    changed = 0
    requisites = (await db.execute(
        select(Requisite)
        .where(
            Requisite.traffic_status.in_([
                TrafficStatus.active.value,
                TrafficStatus.reinstated_limited.value,
                TrafficStatus.reinstated_full.value,
            ])
        )
        .order_by(Requisite.updated_at.asc())
        .limit(limit)
        .with_for_update()
    )).scalars().all()
    for req in requisites:
        stats = await _requisite_window_stats(db, req, window)
        baseline_stats = await _requisite_window_stats(db, req, 1440)
        req_settings = await get_or_create_requisite_settings(db, req.id)
        if stats.total_payments < int(global_settings.min_payments_for_conversion_check or 20):
            continue
        score, reasons = _score_from_stats(
            stats=stats,
            previous_score=int(req.risk_score or 0),
            failed_limit=int(req_settings.failed_payments_in_row_limit or global_settings.failed_payments_in_row_limit_requisite or 5),
            min_conversion_percent=percent(req_settings.min_conversion_percent or global_settings.min_requisite_conversion_percent),
            min_payments_for_conversion_check=int(global_settings.min_payments_for_conversion_check or 20),
            max_confirmation_delay_minutes=int(req_settings.max_confirmation_delay_minutes or global_settings.max_confirmation_delay_minutes or 10),
            merchant_complaints_limit=int(global_settings.merchant_complaints_limit_requisite or 3),
            high_amount=False,
            repeated_after_reinstate=(req.traffic_status == TrafficStatus.reinstated_limited.value),
            baseline_conversion_percent=baseline_stats.conversion_percent,
            conversion_drop_percent_limit=percent(global_settings.conversion_drop_percent_limit),
        )
        req.risk_score = score
        if reasons and score >= 71:
            trader = None
            if req.trader_id:
                trader = (await db.execute(select(User).where(User.id == req.trader_id).with_for_update())).scalar_one_or_none()
            event = await auto_pause_requisite(
                db,
                req=req,
                trader=trader,
                reason=reasons[0],
                score=score,
                stats=stats,
                conversion_before=baseline_stats.conversion_percent,
                window_minutes=window,
            )
            if event:
                changed += 1

    traders = (await db.execute(
        select(User)
        .where(
            User.role.in_([Role.operator.value, Role.trader.value]),
            User.trader_traffic_status.in_([
                TrafficStatus.active.value,
                TrafficStatus.reinstated_limited.value,
                TrafficStatus.reinstated_full.value,
            ]),
        )
        .order_by(User.updated_at.asc())
        .limit(limit)
        .with_for_update()
    )).scalars().all()
    for trader in traders:
        settings = await effective_trader_settings(db, trader)
        stats = await _trader_window_stats(db, trader, settings.conversion_check_window_minutes)
        baseline_stats = await _trader_window_stats(db, trader, 1440)
        if stats.total_payments < int(global_settings.min_payments_for_conversion_check or 20):
            continue
        score, reasons = _score_from_stats(
            stats=stats,
            previous_score=int(trader.trader_risk_score or 0),
            failed_limit=settings.failed_payments_in_row_limit,
            min_conversion_percent=settings.min_conversion_percent,
            min_payments_for_conversion_check=int(global_settings.min_payments_for_conversion_check or 20),
            max_confirmation_delay_minutes=settings.max_confirmation_delay_minutes,
            merchant_complaints_limit=int(global_settings.merchant_complaints_limit_trader or 5),
            high_amount=False,
            repeated_after_reinstate=(trader.trader_traffic_status == TrafficStatus.reinstated_limited.value),
            baseline_conversion_percent=baseline_stats.conversion_percent,
            conversion_drop_percent_limit=settings.conversion_drop_percent_limit,
        )
        trader.trader_risk_score = score
        if reasons and settings.auto_disable_trader_enabled and score >= 86:
            event = await auto_pause_trader(
                db,
                trader=trader,
                reason=reasons[0],
                score=score,
                stats=stats,
                freeze_withdrawals=settings.freeze_withdrawals_on_auto_pause,
                conversion_before=baseline_stats.conversion_percent,
                window_minutes=settings.conversion_check_window_minutes,
            )
            if event:
                changed += 1
    return changed
