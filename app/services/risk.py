from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import BlacklistKind, DepositStatus, PayoutStatus, RiskDecision
from app.models import Appeal, Blacklist, Deposit, LimitRule, Payout, Requisite, RiskEvent


@dataclass
class RiskResult:
    score: int
    decision: RiskDecision
    reason: str
    details: dict


def _day_start() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


async def _blacklisted(db: AsyncSession, kind: BlacklistKind, value: str | None) -> Blacklist | None:
    if not value:
        return None
    return (await db.execute(
        select(Blacklist).where(
            Blacklist.kind == kind.value,
            Blacklist.value == str(value),
            Blacklist.is_active == True,
        )
    )).scalar_one_or_none()


def _destination_variants(value: str | None) -> list[str]:
    if not value:
        return []
    raw = str(value).strip()
    compact = ''.join(ch for ch in raw if ch.isalnum() or ch in '+@')
    digits = ''.join(ch for ch in raw if ch.isdigit())
    variants = [raw, compact, digits]
    result: list[str] = []
    for item in variants:
        if item and item not in result:
            result.append(item)
    return result


async def destination_blacklist_hit(db: AsyncSession, value: str | None) -> Blacklist | None:
    for candidate in _destination_variants(value):
        for kind in [BlacklistKind.requisite, BlacklistKind.card, BlacklistKind.phone]:
            hit = await _blacklisted(db, kind, candidate)
            if hit:
                return hit
    return None


async def assess_operation(
    db: AsyncSession,
    *,
    merchant_id: UUID,
    operation_type: str,
    amount: Decimal,
    method: str,
    ip: str | None = None,
    destination: str | None = None,
) -> RiskResult:
    score = 0
    reasons: list[str] = []
    details: dict = {'method': method, 'amount': str(amount)}

    for kind, value in [
        (BlacklistKind.ip, ip),
        (BlacklistKind.merchant, str(merchant_id)),
    ]:
        hit = await _blacklisted(db, kind, value)
        if hit:
            return RiskResult(100, RiskDecision.deny, f'blacklist hit: {kind.value}', {'blacklist_id': str(hit.id), 'reason': hit.reason})
    destination_hit = await destination_blacklist_hit(db, destination)
    if destination_hit:
        return RiskResult(100, RiskDecision.deny, f'blacklist hit: {destination_hit.kind}', {'blacklist_id': str(destination_hit.id), 'reason': destination_hit.reason})

    rules = (await db.execute(
        select(LimitRule).where(
            or_(LimitRule.merchant_id == merchant_id, LimitRule.merchant_id.is_(None)),
            or_(LimitRule.method == method, LimitRule.method.is_(None)),
        )
    )).scalars().all()
    for rule in rules:
        if amount < rule.min_amount or amount > rule.max_amount:
            return RiskResult(90, RiskDecision.deny, 'amount outside configured limits', {'limit_rule_id': str(rule.id)})
        if rule.daily_amount and rule.daily_amount > 0:
            model = Deposit if operation_type == 'deposit' else Payout
            successful_statuses = [DepositStatus.pending.value, DepositStatus.paid.value] if operation_type == 'deposit' else [PayoutStatus.pending.value, PayoutStatus.processing.value, PayoutStatus.completed.value]
            total = (await db.execute(
                select(func.coalesce(func.sum(model.amount), 0)).where(
                    model.merchant_id == merchant_id,
                    model.method == method,
                    model.status.in_(successful_statuses),
                    model.created_at >= _day_start(),
                )
            )).scalar_one()
            if Decimal(total) + amount > rule.daily_amount:
                return RiskResult(95, RiskDecision.deny, 'daily merchant limit exceeded', {'limit_rule_id': str(rule.id), 'daily_used': str(total)})

    if amount >= Decimal('500000.00'):
        score += 50
        reasons.append('large amount')
    if ip is None:
        score += 10
        reasons.append('missing client ip')
    recent_cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    model = Deposit if operation_type == 'deposit' else Payout
    recent_count = (await db.execute(
        select(func.count()).select_from(model).where(
            model.merchant_id == merchant_id,
            model.created_at >= recent_cutoff,
        )
    )).scalar_one()
    details['recent_merchant_operations_1h'] = int(recent_count)
    if recent_count >= 30:
        score += 20
        reasons.append('high merchant velocity')
    if destination:
        destination_variants = _destination_variants(destination)
        if destination_variants:
            if operation_type == 'deposit':
                req_ids = (await db.execute(
                    select(Requisite.id).where(Requisite.value_encrypted.in_(destination_variants))
                )).scalars().all()
                if req_ids:
                    destination_count = (await db.execute(
                        select(func.count()).select_from(Deposit).where(
                            Deposit.requisites_id.in_(req_ids),
                            Deposit.created_at >= recent_cutoff,
                        )
                    )).scalar_one()
                    details['recent_requisite_operations_1h'] = int(destination_count)
                    if destination_count >= 10:
                        score += 15
                        reasons.append('high requisite velocity')
            else:
                destination_count = (await db.execute(
                    select(func.count()).select_from(Payout).where(
                        Payout.destination == destination,
                        Payout.created_at >= recent_cutoff,
                    )
                )).scalar_one()
                details['recent_destination_payouts_1h'] = int(destination_count)
                if destination_count >= 10:
                    score += 15
                    reasons.append('high destination velocity')
    if amount == amount.quantize(Decimal('1')) and str(amount).endswith('000.00') and amount >= Decimal('10000.00'):
        score += 5
        reasons.append('round amount pattern')
    appeals_count = (await db.execute(
        select(func.count()).select_from(Appeal).where(Appeal.created_at >= datetime.now(timezone.utc) - timedelta(days=1))
    )).scalar_one()
    details['platform_appeals_24h'] = int(appeals_count)
    if appeals_count >= 50:
        score += 10
        reasons.append('high appeal volume')

    decision = RiskDecision.review if score >= 50 else RiskDecision.allow
    return RiskResult(score, decision, ', '.join(reasons) or 'passed', details)


async def record_risk_event(
    db: AsyncSession,
    *,
    merchant_id: UUID | None,
    operation_type: str,
    operation_id: UUID | None,
    result: RiskResult,
) -> RiskEvent:
    event = RiskEvent(
        merchant_id=merchant_id,
        operation_type=operation_type,
        operation_id=operation_id,
        score=result.score,
        decision=result.decision.value,
        reason=result.reason,
        details=result.details,
    )
    db.add(event)
    await db.flush()
    return event
