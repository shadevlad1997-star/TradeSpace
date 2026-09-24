from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.compliance import COMPLIANCE_POLICY, KYC_REQUIREMENTS
from app.core.security import encrypt_secret
from app.models import Blacklist, Deposit, KycProfile, Merchant, Payout, RiskEvent, WebhookEvent


VALID_RISK_LEVELS = set(KYC_REQUIREMENTS.keys())


async def get_compliance_summary(db: AsyncSession) -> dict:
    kyc_rows = (await db.execute(
        select(KycProfile.status, func.count()).group_by(KycProfile.status)
    )).all()
    risk_rows = (await db.execute(
        select(RiskEvent.decision, func.count()).group_by(RiskEvent.decision)
    )).all()
    blacklist_count = (await db.execute(
        select(func.count()).select_from(Blacklist).where(Blacklist.is_active == True)
    )).scalar_one()
    webhook_failures = (await db.execute(
        select(func.count()).select_from(WebhookEvent).where(WebhookEvent.status.in_(['failed', 'blocked']))
    )).scalar_one()
    merchants_total = (await db.execute(select(func.count()).select_from(Merchant))).scalar_one()
    merchants_approved = (await db.execute(
        select(func.count()).select_from(KycProfile).where(KycProfile.status == 'approved')
    )).scalar_one()
    return {
        'policy': COMPLIANCE_POLICY,
        'merchants_total': merchants_total,
        'merchants_kyc_approved': merchants_approved,
        'kyc_statuses': {status: count for status, count in kyc_rows},
        'risk_decisions': {decision: count for decision, count in risk_rows},
        'active_blacklist_items': blacklist_count,
        'webhook_failures_or_blocks': webhook_failures,
    }


async def list_kyc_profiles(db: AsyncSession, *, status: str | None = None, risk_level: str | None = None) -> list[KycProfile]:
    filters = []
    if status:
        filters.append(KycProfile.status == status)
    if risk_level:
        filters.append(KycProfile.risk_level == risk_level)
    return (await db.execute(
        select(KycProfile).where(*filters).order_by(KycProfile.created_at.desc()).limit(500)
    )).scalars().all()


async def get_or_create_kyc_profile(db: AsyncSession, merchant_id: UUID) -> KycProfile:
    merchant = (await db.execute(select(Merchant).where(Merchant.id == merchant_id))).scalar_one_or_none()
    if not merchant:
        raise ValueError('merchant not found')
    profile = (await db.execute(select(KycProfile).where(KycProfile.merchant_id == merchant_id))).scalar_one_or_none()
    if profile:
        return profile
    profile = KycProfile(merchant_id=merchant_id, status='not_started', risk_level='standard')
    db.add(profile)
    await db.flush()
    return profile


async def review_kyc_profile(
    db: AsyncSession,
    *,
    merchant_id: UUID,
    status: str,
    risk_level: str,
    legal_name: str | None,
    tax_id: str | None,
    country: str,
    reviewed_by: UUID,
) -> KycProfile:
    if risk_level not in VALID_RISK_LEVELS:
        raise ValueError('invalid risk_level')
    profile = await get_or_create_kyc_profile(db, merchant_id)
    profile.status = status
    profile.risk_level = risk_level
    profile.country = country.upper()
    profile.reviewed_by = reviewed_by
    profile.reviewed_at = datetime.now(timezone.utc)
    if legal_name is not None:
        profile.legal_name = legal_name
    if tax_id:
        profile.tax_id_encrypted = encrypt_secret(tax_id)
    await db.flush()
    return profile


async def merchant_compliance_snapshot(db: AsyncSession, *, merchant_id: UUID) -> dict:
    merchant = (await db.execute(select(Merchant).where(Merchant.id == merchant_id))).scalar_one_or_none()
    if not merchant:
        raise ValueError('merchant not found')
    profile = await get_or_create_kyc_profile(db, merchant_id)
    risk_counts = (await db.execute(
        select(RiskEvent.decision, func.count()).where(RiskEvent.merchant_id == merchant_id).group_by(RiskEvent.decision)
    )).all()
    recent_risks = (await db.execute(
        select(RiskEvent).where(RiskEvent.merchant_id == merchant_id).order_by(RiskEvent.created_at.desc()).limit(20)
    )).scalars().all()
    deposits_count = (await db.execute(select(func.count()).select_from(Deposit).where(Deposit.merchant_id == merchant_id))).scalar_one()
    payouts_count = (await db.execute(select(func.count()).select_from(Payout).where(Payout.merchant_id == merchant_id))).scalar_one()
    merchant_blacklisted = (await db.execute(
        select(Blacklist).where(Blacklist.kind == 'merchant', Blacklist.value == str(merchant_id), Blacklist.is_active == True)
    )).scalar_one_or_none()
    return {
        'merchant_id': merchant.id,
        'merchant_name': merchant.name,
        'sandbox_mode': merchant.sandbox_mode,
        'kyc_profile': profile,
        'required_documents': KYC_REQUIREMENTS.get(profile.risk_level, KYC_REQUIREMENTS['standard'])['required_documents'],
        'risk_decisions': {decision: count for decision, count in risk_counts},
        'recent_risk_events': recent_risks,
        'operations': {'deposits': deposits_count, 'payouts': payouts_count},
        'blacklisted': merchant_blacklisted is not None,
    }
