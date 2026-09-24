from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import LedgerType
from app.models import Balance, LedgerEntry, PlatformLedgerEntry, TraderLedgerEntry, User


TWOPLACES = Decimal('0.01')


def money(value) -> Decimal:
    return Decimal(value or '0.00').quantize(TWOPLACES, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class ReconciliationResult:
    ok: bool
    actual_available: Decimal
    actual_frozen: Decimal
    expected_available: Decimal
    expected_frozen: Decimal
    obligation_frozen: Decimal = Decimal('0.00')
    obligation_delta: Decimal = Decimal('0.00')
    issues: tuple[str, ...] = ()

    @property
    def available_delta(self) -> Decimal:
        return money(self.actual_available - self.expected_available)

    @property
    def frozen_delta(self) -> Decimal:
        return money(self.actual_frozen - self.expected_frozen)


async def reconcile_merchant_balance(db: AsyncSession, merchant_id: UUID, currency: str = 'RUB') -> ReconciliationResult:
    # ONE statement => ONE MVCC snapshot even under READ COMMITTED. No locks,
    # commits, or autoflush: a dashboard must not mutate its caller's session.
    query = text("""
        SELECT (SELECT json_build_object('available', available::text, 'frozen', frozen::text)
                FROM balances WHERE merchant_id=:owner AND currency=:currency) AS balance,
               (SELECT json_agg(json_build_object('type',entry_type,'amount',amount::text,'operation',operation_id))
                FROM ledger_entries WHERE merchant_id=:owner AND currency=:currency) AS entries,
               (SELECT coalesce(sum(amount),0) FROM payouts WHERE merchant_id=:owner AND currency=:currency
                AND status IN ('pending','processing','appeal_opened')) +
               (SELECT coalesce(sum(total_debit_rub),0) FROM merchant_settlements
                WHERE merchant_id=:owner AND :currency='RUB' AND status='pending') AS obligations
    """)
    with db.no_autoflush:
        row = (await db.execute(query, {'owner':merchant_id,'currency':currency})).mappings().one()
    balance = row['balance'] or {}; entries = row['entries'] or []
    held_targets = {e['operation'] for e in entries if e['type']=='hold'}
    available = frozen = Decimal('0.00'); issues = []
    for entry in entries:
        amount=money(entry['amount']); kind=entry['type']
        if kind=='credit': available += amount
        elif kind=='hold': available -= amount; frozen += amount
        elif kind=='release': available += amount; frozen -= amount
        elif kind=='debit':
            if entry['operation'] in held_targets: frozen -= amount
            else: available -= amount
        elif kind!='fee': issues.append('unknown_ledger_entry_type')
    actual_available=money(balance.get('available')); actual_frozen=money(balance.get('frozen'))
    obligations=money(row['obligations'])
    if actual_frozen!=obligations: issues.append('active_obligation_reserve_mismatch')
    if actual_available!=money(available) or actual_frozen!=money(frozen): issues.append('ledger_balance_mismatch')
    return ReconciliationResult(not issues,actual_available,actual_frozen,money(available),money(frozen),
                                obligations,money(actual_frozen-obligations),tuple(sorted(set(issues))))


async def reconcile_trader_balance(db: AsyncSession, trader_id: UUID) -> ReconciliationResult:
    query = text("""
        SELECT (SELECT json_build_object('balance',trader_balance::text,'hold',trader_hold::text)
                FROM users WHERE id=:owner) AS balance,
               (SELECT json_agg(json_build_object('type',entry_type,'amount',amount::text,'key',idempotency_key,
                                                 'balance_after',balance_after::text,'hold_after',hold_after::text)
                                ORDER BY created_at,id)
                FROM trader_ledger_entries WHERE trader_id=:owner AND currency='RUB') AS entries,
               (SELECT json_agg(json_build_object('amount',d.amount::text,'status',d.status,'metadata',d.metadata_json))
                FROM deposits d JOIN requisites r ON r.id=d.requisites_id
                WHERE r.trader_id=:owner AND d.currency='RUB'
                  AND (d.status IN ('created','pending','processing','appeal_opened')
                       OR d.metadata_json->>'trader_hold_status'='active')) AS deposits
    """)
    with db.no_autoflush:
        row=(await db.execute(query,{'owner':trader_id})).mappings().one()
    balance=row['balance'] or {}; entries=row['entries'] or []
    expected_balance=expected_hold=manual_hold=obligations=Decimal('0.00'); issues=[]
    for e in entries:
        amount=money(e['amount']); kind=e['type']
        if kind=='balance_adjustment': expected_balance += amount
        elif kind=='insurance_deposit_set': expected_hold += amount; manual_hold += amount
        elif kind=='hold': expected_hold += amount
        elif kind=='release_hold': expected_hold -= amount
        elif kind=='deposit_success_debit':
            expected_balance -= amount
            if not str(e['key'] or '').startswith('trader-appeal-available-debit:'): expected_hold -= amount
        else: issues.append('unknown_ledger_entry_type')
    for deposit in row['deposits'] or []:
        meta=deposit['metadata'] or {}
        if meta.get('trader_hold_status') in {'released','settled'} or meta.get('trader_hold_released') or meta.get('trader_settled'):
            continue
        try:
            obligation=money(meta.get('trader_hold_amount') or meta.get('trader_settlement_amount') or deposit['amount'])
            if not obligation.is_finite() or obligation < 0: raise ValueError()
            obligations += obligation
        except (ValueError, ArithmeticError): issues.append('invalid_operation_reserve')
        if deposit['status'] not in {'created','pending','processing','appeal_opened'}:
            issues.append('terminal_operation_has_active_reserve')
    # Manual insurance is an independent, journalled reserve source. It must
    # not be mistaken for collateral of any particular Deposit.
    obligations += manual_hold
    actual_frozen=money(balance.get('hold')); actual_available=money(balance.get('balance'))-actual_frozen
    if actual_frozen!=money(obligations): issues.append('active_obligation_reserve_mismatch')
    if actual_available!=money(expected_balance-expected_hold) or actual_frozen!=money(expected_hold): issues.append('ledger_balance_mismatch')
    return ReconciliationResult(not issues,actual_available,actual_frozen,money(expected_balance-expected_hold),money(expected_hold),
                                money(obligations),money(actual_frozen-obligations),tuple(sorted(set(issues))))


async def platform_income_total(db: AsyncSession, merchant_id: UUID | None = None) -> Decimal:
    query = select(PlatformLedgerEntry).where(PlatformLedgerEntry.entry_type == 'platform_income')
    if merchant_id is not None:
        query = query.where(PlatformLedgerEntry.merchant_id == merchant_id)
    rows = (await db.execute(query)).scalars().all()
    return money(sum((money(row.amount) for row in rows), Decimal('0.00')))
