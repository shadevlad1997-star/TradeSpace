import hashlib
import re
from decimal import Decimal, InvalidOperation

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Deposit, SmsMessage
from app.core.enums import DepositStatus
from app.services.fees import settle_deposit_credit
from app.services.antiscam import check_payment_result_for_risk
from app.services.deposit_lifecycle import finalize_unsuccessful_deposit
from app.services.deposit_ttl import deposit_is_expired
from app.services.requisites import settle_trader_deposit_balance

AMOUNT_MARKERS = (
    'поступление',
    'перевод',
    'зачисление',
    'пополнение',
    'payment received',
    'transfer received',
)
BALANCE_MARKERS = ('баланс', 'balance', 'остаток', 'доступно')
CARD_LAST4_RE = re.compile(r'(?:\*{2,}|x{2,}|карта|card)\s*([0-9]{4})', re.I)
AMOUNT_WITH_CURRENCY_RE = re.compile(r'([0-9][0-9\s]{0,12}(?:[\.,][0-9]{1,2})?)\s*(?:rub|руб|р|₽)', re.I)
NUMBER_RE = re.compile(r'[0-9][0-9\s]{0,12}(?:[\.,][0-9]{1,2})?')


def _money(raw: str) -> Decimal | None:
    try:
        value = Decimal(raw.replace(' ', '').replace(',', '.')).quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError):
        return None
    if value <= 0:
        return None
    return value


def parse_amount_with_confidence(text: str) -> tuple[Decimal | None, Decimal]:
    clean = ' '.join((text or '').replace('\xa0', ' ').split())
    lowered = clean.lower()
    if not clean:
        return None, Decimal('0.00')

    card_last4 = {match.group(1) for match in CARD_LAST4_RE.finditer(clean)}
    best_amount: Decimal | None = None
    best_confidence = Decimal('0.00')

    for marker in AMOUNT_MARKERS:
        marker_pos = lowered.find(marker)
        if marker_pos < 0:
            continue
        window = clean[marker_pos: marker_pos + 120]
        for match in AMOUNT_WITH_CURRENCY_RE.finditer(window):
            amount = _money(match.group(1))
            if amount is None or str(int(amount)) in card_last4:
                continue
            prefix = window[max(0, match.start() - 30):match.start()].lower()
            if any(balance_marker in prefix for balance_marker in BALANCE_MARKERS):
                continue
            best_amount = amount
            best_confidence = Decimal('0.95')
            break
        if best_amount is not None:
            break

    if best_amount is None:
        for match in AMOUNT_WITH_CURRENCY_RE.finditer(clean):
            amount = _money(match.group(1))
            if amount is None or str(int(amount)) in card_last4:
                continue
            prefix = lowered[max(0, match.start() - 30):match.start()]
            if any(marker in prefix for marker in BALANCE_MARKERS):
                continue
            best_amount = amount
            best_confidence = Decimal('0.70')
            break

    if best_amount is None:
        # Plain numbers without currency are accepted only near explicit incoming-payment markers.
        for marker in AMOUNT_MARKERS:
            marker_pos = lowered.find(marker)
            if marker_pos < 0:
                continue
            window = clean[marker_pos: marker_pos + 80]
            for match in NUMBER_RE.finditer(window):
                amount = _money(match.group(0))
                if amount is None or str(int(amount)) in card_last4:
                    continue
                best_amount = amount
                best_confidence = Decimal('0.55')
                break
            if best_amount is not None:
                break

    return best_amount, best_confidence


def parse_amount(text: str) -> Decimal | None:
    amount, confidence = parse_amount_with_confidence(text)
    return amount if confidence >= Decimal('0.70') else None


async def ingest_sms(db: AsyncSession, provider_message_id: str, sender: str, body: str):
    normalized = f'{provider_message_id}|{sender}|{body}'.encode('utf-8', errors='ignore')
    message_hash = hashlib.sha256(normalized).hexdigest()
    existing = (await db.execute(
        select(SmsMessage).where(
            or_(
                SmsMessage.provider_message_id == provider_message_id,
                SmsMessage.message_hash == message_hash,
            )
        )
    )).scalar_one_or_none()
    if existing:
        return existing
    amount, confidence = parse_amount_with_confidence(body)
    review_required = amount is None or confidence < Decimal('0.70')
    sms = SmsMessage(
        provider_message_id=provider_message_id,
        sender=sender,
        body=body,
        parsed_amount=amount if not review_required else None,
        message_hash=message_hash,
        parse_confidence=confidence,
        review_required=review_required,
    )
    db.add(sms)
    await db.flush()
    return sms


async def confirm_deposit_from_sms(
    db: AsyncSession,
    sms_id,
    deposit_id,
    *,
    actor_id=None,
    actor_ip: str | None = None,
):
    dep = (await db.execute(select(Deposit).where(Deposit.id == deposit_id).with_for_update())).scalar_one_or_none()
    sms = (await db.execute(select(SmsMessage).where(SmsMessage.id == sms_id).with_for_update())).scalar_one_or_none()
    if not sms or not dep:
        raise LookupError('sms or deposit not found')
    if sms.processed and sms.linked_deposit_id:
        if sms.linked_deposit_id == dep.id:
            return sms, dep, False, None
        raise ValueError('sms is already linked to another deposit')
    if sms.parsed_amount is not None and sms.parsed_amount != dep.amount:
        raise ValueError('sms amount does not match deposit amount')
    if dep.status in [DepositStatus.created.value, DepositStatus.pending.value, DepositStatus.appeal_opened.value]:
        if deposit_is_expired(dep):
            finalization = await finalize_unsuccessful_deposit(
                db,
                dep,
                reason='trader_timeout',
                actor_id=actor_id,
                actor_role='superadmin',
                actor_ip=actor_ip,
                audit_action='sms_confirm_rejected_after_ttl',
            )
            return sms, dep, False, finalization
        await settle_trader_deposit_balance(db, dep)
        dep.status = DepositStatus.paid.value
        await settle_deposit_credit(db, merchant_id=dep.merchant_id, method=dep.method, amount=dep.amount, operation_id=dep.id, description='manual SMS confirmation')
        await check_payment_result_for_risk(db, dep)
    elif dep.status != DepositStatus.paid.value:
        raise ValueError(f'deposit status {dep.status} cannot be confirmed by sms')
    sms.linked_deposit_id = dep.id
    sms.processed = True
    sms.review_required = False
    return sms, dep, True, None
