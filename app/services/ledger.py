from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Balance, LedgerEntry, PlatformLedgerEntry, TraderLedgerEntry, User
from app.core.enums import LedgerType

class LedgerError(Exception): pass

TWOPLACES = Decimal('0.01')


def _money(value, *, allow_zero: bool = False) -> Decimal:
    try:
        amount = Decimal(value if value is not None else '0.00').quantize(TWOPLACES, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise LedgerError('invalid amount') from exc
    if allow_zero:
        if amount < Decimal('0.00'):
            raise LedgerError('amount must not be negative')
    elif amount <= Decimal('0.00'):
        raise LedgerError('amount must be positive')
    return amount


def _same_id(left, right) -> bool:
    if left is None and right is None:
        return True
    if left is None or right is None:
        return False
    return str(left) == str(right)


def _assert_merchant_entry(existing: LedgerEntry, *, merchant_id, amount: Decimal, entry_type: str, operation_id=None) -> None:
    if not _same_id(existing.merchant_id, merchant_id):
        raise LedgerError('ledger idempotency target mismatch')
    if existing.entry_type != entry_type:
        raise LedgerError('ledger idempotency type mismatch')
    if _money(existing.amount) != amount:
        raise LedgerError('ledger idempotency amount mismatch')
    if operation_id is not None and not _same_id(existing.operation_id, operation_id):
        raise LedgerError('ledger idempotency operation mismatch')


def _assert_trader_entry(existing: TraderLedgerEntry, *, trader: User, amount: Decimal, entry_type: str, operation_id=None) -> None:
    if not _same_id(existing.trader_id, trader.id):
        raise LedgerError('trader ledger idempotency target mismatch')
    if existing.entry_type != entry_type:
        raise LedgerError('trader ledger idempotency type mismatch')
    if Decimal(existing.amount or '0.00').quantize(TWOPLACES, rounding=ROUND_HALF_UP) != amount:
        raise LedgerError('trader ledger idempotency amount mismatch')
    if operation_id is not None and not _same_id(existing.operation_id, operation_id):
        raise LedgerError('trader ledger idempotency operation mismatch')


def _assert_platform_entry(existing: PlatformLedgerEntry, *, merchant_id, amount: Decimal, entry_type: str, operation_id=None) -> None:
    if merchant_id is not None and not _same_id(existing.merchant_id, merchant_id):
        raise LedgerError('platform ledger idempotency target mismatch')
    if existing.entry_type != entry_type:
        raise LedgerError('platform ledger idempotency type mismatch')
    if _money(existing.amount) != amount:
        raise LedgerError('platform ledger idempotency amount mismatch')
    if operation_id is not None and not _same_id(existing.operation_id, operation_id):
        raise LedgerError('platform ledger idempotency operation mismatch')


async def _find_merchant_entry(db: AsyncSession, idempotency_key):
    if not idempotency_key:
        return None
    return (await db.execute(select(LedgerEntry).where(LedgerEntry.idempotency_key == idempotency_key))).scalar_one_or_none()


async def _find_trader_entry(db: AsyncSession, idempotency_key):
    if not idempotency_key:
        return None
    return (await db.execute(select(TraderLedgerEntry).where(TraderLedgerEntry.idempotency_key == idempotency_key))).scalar_one_or_none()


async def _find_platform_entry(db: AsyncSession, idempotency_key):
    if not idempotency_key:
        return None
    return (await db.execute(select(PlatformLedgerEntry).where(PlatformLedgerEntry.idempotency_key == idempotency_key))).scalar_one_or_none()

async def get_or_create_balance(db: AsyncSession, merchant_id, currency='RUB') -> Balance:
    res = await db.execute(select(Balance).where(Balance.merchant_id==merchant_id, Balance.currency==currency).with_for_update())
    bal = res.scalar_one_or_none()
    if not bal:
        bal = Balance(merchant_id=merchant_id, currency=currency)
        db.add(bal); await db.flush()
    return bal

async def credit(db, merchant_id, amount: Decimal, operation_id=None, idempotency_key=None, description=''):
    amount = _money(amount)
    existing = await _find_merchant_entry(db, idempotency_key)
    if existing:
        _assert_merchant_entry(existing, merchant_id=merchant_id, amount=amount, entry_type=LedgerType.credit.value, operation_id=operation_id)
        return existing
    bal = await get_or_create_balance(db, merchant_id)
    bal.available += amount
    entry = LedgerEntry(merchant_id=merchant_id, operation_id=operation_id, entry_type=LedgerType.credit.value, amount=amount, idempotency_key=idempotency_key, description=description)
    db.add(entry)
    return entry

async def hold(db, merchant_id, amount: Decimal, operation_id=None, idempotency_key=None, description=''):
    amount = _money(amount)
    existing = await _find_merchant_entry(db, idempotency_key)
    if existing:
        _assert_merchant_entry(existing, merchant_id=merchant_id, amount=amount, entry_type=LedgerType.hold.value, operation_id=operation_id)
        return existing
    bal = await get_or_create_balance(db, merchant_id)
    if bal.available < amount:
        raise LedgerError('insufficient funds')
    bal.available -= amount; bal.frozen += amount
    entry = LedgerEntry(merchant_id=merchant_id, operation_id=operation_id, entry_type=LedgerType.hold.value, amount=amount, idempotency_key=idempotency_key, description=description)
    db.add(entry)
    return entry

async def release_hold(db, merchant_id, amount: Decimal, operation_id=None, idempotency_key=None, description=''):
    amount = _money(amount)
    existing = await _find_merchant_entry(db, idempotency_key)
    if existing:
        _assert_merchant_entry(existing, merchant_id=merchant_id, amount=amount, entry_type=LedgerType.release.value, operation_id=operation_id)
        return existing
    bal = await get_or_create_balance(db, merchant_id)
    if bal.frozen < amount:
        raise LedgerError('invalid frozen amount')
    bal.frozen -= amount; bal.available += amount
    entry = LedgerEntry(merchant_id=merchant_id, operation_id=operation_id, entry_type=LedgerType.release.value, amount=amount, idempotency_key=idempotency_key, description=description)
    db.add(entry)
    return entry

async def debit_frozen(db, merchant_id, amount: Decimal, operation_id=None, idempotency_key=None, description=''):
    amount = _money(amount)
    existing = await _find_merchant_entry(db, idempotency_key)
    if existing:
        _assert_merchant_entry(existing, merchant_id=merchant_id, amount=amount, entry_type=LedgerType.debit.value, operation_id=operation_id)
        return existing
    bal = await get_or_create_balance(db, merchant_id)
    if bal.frozen < amount:
        raise LedgerError('invalid frozen amount')
    bal.frozen -= amount
    entry = LedgerEntry(merchant_id=merchant_id, operation_id=operation_id, entry_type=LedgerType.debit.value, amount=amount, idempotency_key=idempotency_key, description=description)
    db.add(entry)
    return entry


async def debit_available(db, merchant_id, amount: Decimal, operation_id=None, idempotency_key=None, description=''):
    amount = _money(amount)
    existing = await _find_merchant_entry(db, idempotency_key)
    if existing:
        _assert_merchant_entry(existing, merchant_id=merchant_id, amount=amount, entry_type=LedgerType.debit.value, operation_id=operation_id)
        return existing
    bal = await get_or_create_balance(db, merchant_id)
    if bal.available < amount:
        raise LedgerError('insufficient funds')
    bal.available -= amount
    entry = LedgerEntry(merchant_id=merchant_id, operation_id=operation_id, entry_type=LedgerType.debit.value, amount=amount, idempotency_key=idempotency_key, description=description)
    db.add(entry)
    return entry
async def record_fee(db, merchant_id, amount: Decimal, operation_id=None, idempotency_key=None, description=''):
    amount = _money(amount, allow_zero=True)
    if amount == 0:
        return None
    existing = await _find_merchant_entry(db, idempotency_key)
    if existing:
        _assert_merchant_entry(existing, merchant_id=merchant_id, amount=amount, entry_type=LedgerType.fee.value, operation_id=operation_id)
        return existing
    entry = LedgerEntry(
        merchant_id=merchant_id,
        operation_id=operation_id,
        entry_type=LedgerType.fee.value,
        amount=amount,
        idempotency_key=idempotency_key,
        description=description,
    )
    db.add(entry)
    return entry


async def record_platform_income(db, merchant_id, amount: Decimal, operation_id=None, idempotency_key=None, description=''):
    amount = _money(amount, allow_zero=True)
    if amount == 0:
        return None
    existing = await _find_platform_entry(db, idempotency_key)
    if existing:
        _assert_platform_entry(existing, merchant_id=merchant_id, amount=amount, entry_type='platform_income', operation_id=operation_id)
        return existing
    entry = PlatformLedgerEntry(
        merchant_id=merchant_id,
        operation_id=operation_id,
        entry_type='platform_income',
        amount=amount,
        idempotency_key=idempotency_key,
        description=description,
    )
    db.add(entry)
    return entry


async def record_platform_teamlead_expense(
    db,
    merchant_id,
    amount: Decimal,
    operation_id=None,
    idempotency_key=None,
    description='',
):
    amount = _money(amount, allow_zero=True)
    if amount == 0:
        return None
    existing = await _find_platform_entry(db, idempotency_key)
    if existing:
        _assert_platform_entry(
            existing,
            merchant_id=merchant_id,
            amount=amount,
            entry_type='teamlead_expense',
            operation_id=operation_id,
        )
        return existing
    entry = PlatformLedgerEntry(
        merchant_id=merchant_id,
        operation_id=operation_id,
        entry_type='teamlead_expense',
        amount=amount,
        idempotency_key=idempotency_key,
        description=description,
    )
    db.add(entry)
    return entry


async def record_platform_teamlead_expense_reversal(
    db,
    merchant_id,
    amount: Decimal,
    operation_id=None,
    idempotency_key=None,
    description='',
):
    amount = _money(amount, allow_zero=True)
    if amount == 0:
        return None
    existing = await _find_platform_entry(db, idempotency_key)
    if existing:
        _assert_platform_entry(
            existing,
            merchant_id=merchant_id,
            amount=amount,
            entry_type='teamlead_expense_reversal',
            operation_id=operation_id,
        )
        return existing
    entry = PlatformLedgerEntry(
        merchant_id=merchant_id,
        operation_id=operation_id,
        entry_type='teamlead_expense_reversal',
        amount=amount,
        idempotency_key=idempotency_key,
        description=description,
    )
    db.add(entry)
    return entry


async def record_platform_executor_fee(db, merchant_id, amount: Decimal, operation_id=None, idempotency_key=None, description=''):
    amount = _money(amount, allow_zero=True)
    if amount == 0:
        return None
    existing = await _find_platform_entry(db, idempotency_key)
    if existing:
        _assert_platform_entry(
            existing,
            merchant_id=merchant_id,
            amount=amount,
            entry_type='executor_fee',
            operation_id=operation_id,
        )
        return existing
    entry = PlatformLedgerEntry(
        merchant_id=merchant_id,
        operation_id=operation_id,
        entry_type='executor_fee',
        amount=amount,
        idempotency_key=idempotency_key,
        description=description,
    )
    db.add(entry)
    return entry


async def record_trader_ledger(db, trader: User, entry_type: str, amount: Decimal, operation_id=None, idempotency_key=None, description=''):
    amount = Decimal(amount or '0.00').quantize(TWOPLACES, rounding=ROUND_HALF_UP)
    existing = await _find_trader_entry(db, idempotency_key)
    if existing:
        _assert_trader_entry(existing, trader=trader, amount=amount, entry_type=entry_type, operation_id=operation_id)
        return existing
    entry = TraderLedgerEntry(
        trader_id=trader.id,
        operation_id=operation_id,
        entry_type=entry_type,
        amount=amount,
        balance_after=trader.trader_balance,
        hold_after=trader.trader_hold,
        idempotency_key=idempotency_key,
        description=description,
    )
    db.add(entry)
    return entry


async def trader_hold(db, trader: User, amount: Decimal, operation_id=None, idempotency_key=None, description=''):
    amount = _money(amount)
    existing = await _find_trader_entry(db, idempotency_key)
    if existing:
        _assert_trader_entry(existing, trader=trader, amount=amount, entry_type='hold', operation_id=operation_id)
        return existing
    available = Decimal(trader.trader_balance or '0.00') - Decimal(trader.trader_hold or '0.00')
    if available < amount:
        raise LedgerError('insufficient trader available balance')
    trader.trader_hold += amount
    return await record_trader_ledger(db, trader, 'hold', amount, operation_id, idempotency_key, description)


async def trader_release_hold(db, trader: User, amount: Decimal, operation_id=None, idempotency_key=None, description=''):
    amount = _money(amount)
    existing = await _find_trader_entry(db, idempotency_key)
    if existing:
        _assert_trader_entry(existing, trader=trader, amount=amount, entry_type='release_hold', operation_id=operation_id)
        return existing
    if Decimal(trader.trader_hold or '0.00') < amount:
        raise LedgerError('invalid trader frozen amount')
    trader.trader_hold -= amount
    return await record_trader_ledger(db, trader, 'release_hold', amount, operation_id, idempotency_key, description)


async def trader_debit_frozen(db, trader: User, amount: Decimal, operation_id=None, idempotency_key=None, description=''):
    amount = _money(amount)
    existing = await _find_trader_entry(db, idempotency_key)
    if existing:
        _assert_trader_entry(existing, trader=trader, amount=amount, entry_type='deposit_success_debit', operation_id=operation_id)
        return existing
    if Decimal(trader.trader_hold or '0.00') < amount:
        raise LedgerError('invalid trader frozen amount')
    if Decimal(trader.trader_balance or '0.00') < amount:
        raise LedgerError('invalid trader balance')
    trader.trader_hold -= amount
    trader.trader_balance -= amount
    return await record_trader_ledger(db, trader, 'deposit_success_debit', amount, operation_id, idempotency_key, description)


async def trader_debit_available(db, trader: User, amount: Decimal, operation_id=None, idempotency_key=None, description=''):
    amount = _money(amount)
    existing = await _find_trader_entry(db, idempotency_key)
    if existing:
        _assert_trader_entry(existing, trader=trader, amount=amount, entry_type='deposit_success_debit', operation_id=operation_id)
        return existing
    available = Decimal(trader.trader_balance or '0.00') - Decimal(trader.trader_hold or '0.00')
    if available < amount:
        raise LedgerError('insufficient trader available balance')
    trader.trader_balance -= amount
    return await record_trader_ledger(db, trader, 'deposit_success_debit', amount, operation_id, idempotency_key, description)


async def manual_trader_adjustment(
    db,
    trader: User,
    *,
    target_balance: Decimal,
    target_hold: Decimal,
    operation_id=None,
    idempotency_prefix: str,
    reason: str,
):
    if not (reason or '').strip():
        raise LedgerError('manual trader adjustment reason is required')
    target_balance = _money(target_balance, allow_zero=True)
    target_hold = _money(target_hold, allow_zero=True)
    if target_hold > target_balance:
        raise LedgerError('trader hold cannot exceed trader balance')
    previous_balance = _money(trader.trader_balance, allow_zero=True)
    previous_hold = _money(trader.trader_hold, allow_zero=True)
    entries = []
    if target_balance != previous_balance:
        trader.trader_balance = target_balance
        entries.append(await record_trader_ledger(
            db,
            trader,
            'balance_adjustment',
            target_balance - previous_balance,
            operation_id,
            f'{idempotency_prefix}:balance',
            reason,
        ))
    if target_hold != previous_hold:
        trader.trader_hold = target_hold
        entries.append(await record_trader_ledger(
            db,
            trader,
            'insurance_deposit_set',
            target_hold - previous_hold,
            operation_id,
            f'{idempotency_prefix}:hold',
            reason,
        ))
    return entries
