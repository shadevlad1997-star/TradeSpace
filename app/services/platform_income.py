from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AggregatorAccount,
    Deposit,
    Merchant,
    OperationFeeSnapshot,
    PlatformLedgerEntry,
    TeamLeadAccrual,
    TeamLeadMerchantAccrual,
    User,
)
from app.services.fee_tiers import money


FAILED_OPERATION_STATUSES = ('failed', 'cancelled', 'expired', 'rejected')


async def platform_income_dashboard(
    db: AsyncSession,
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    merchant_id: UUID | None = None,
    trader_id: UUID | None = None,
    aggregator_id: UUID | None = None,
    payment_method: str | None = None,
    currency: str | None = None,
    executor_type: str | None = None,
    operation_status: str | None = None,
    settlement_status: str | None = None,
    amount_min: Decimal | None = None,
    amount_max: Decimal | None = None,
    margin: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    conditions = []
    if date_from is not None:
        conditions.append(OperationFeeSnapshot.rate_snapshot_at >= date_from)
    if date_to is not None:
        conditions.append(OperationFeeSnapshot.rate_snapshot_at < date_to)
    if merchant_id is not None:
        conditions.append(Deposit.merchant_id == merchant_id)
    if trader_id is not None:
        conditions.extend((
            OperationFeeSnapshot.executor_type == 'trader',
            OperationFeeSnapshot.executor_id == trader_id,
        ))
    if aggregator_id is not None:
        conditions.extend((
            OperationFeeSnapshot.executor_type == 'aggregator',
            OperationFeeSnapshot.executor_id == aggregator_id,
        ))
    if payment_method:
        conditions.append(OperationFeeSnapshot.payment_method == payment_method)
    if currency:
        conditions.append(OperationFeeSnapshot.currency == currency.upper())
    if executor_type:
        conditions.append(OperationFeeSnapshot.executor_type == executor_type)
    if operation_status:
        conditions.append(Deposit.status == operation_status)
    if settlement_status:
        conditions.append(OperationFeeSnapshot.settlement_status == settlement_status)
    if amount_min is not None:
        conditions.append(OperationFeeSnapshot.calculation_base_amount >= amount_min)
    if amount_max is not None:
        conditions.append(OperationFeeSnapshot.calculation_base_amount <= amount_max)
    if margin == 'positive':
        conditions.append(OperationFeeSnapshot.platform_income_amount > 0)
    elif margin == 'zero':
        conditions.append(OperationFeeSnapshot.platform_income_amount == 0)
    elif margin == 'negative':
        conditions.append(OperationFeeSnapshot.platform_income_amount < 0)

    financial_conditions = [*conditions, OperationFeeSnapshot.settlement_status == 'settled']
    base = (
        select(OperationFeeSnapshot, Deposit, Merchant)
        .join(Deposit, Deposit.id == OperationFeeSnapshot.deposit_id)
        .join(Merchant, Merchant.id == Deposit.merchant_id)
        .where(*conditions)
    )
    all_aggregate = (await db.execute(
        select(
            func.count(OperationFeeSnapshot.id),
            func.coalesce(func.sum(OperationFeeSnapshot.calculation_base_amount), 0),
        )
        .select_from(OperationFeeSnapshot)
        .join(Deposit, Deposit.id == OperationFeeSnapshot.deposit_id)
        .where(*conditions)
    )).one()
    financial_aggregate = (await db.execute(
        select(
            func.count(OperationFeeSnapshot.id),
            func.coalesce(func.sum(OperationFeeSnapshot.calculation_base_amount), 0),
            func.coalesce(func.sum(OperationFeeSnapshot.merchant_fee_amount), 0),
            func.coalesce(func.sum(OperationFeeSnapshot.executor_fee_amount), 0),
            func.coalesce(func.sum(OperationFeeSnapshot.platform_income_amount), 0),
        )
        .select_from(OperationFeeSnapshot)
        .join(Deposit, Deposit.id == OperationFeeSnapshot.deposit_id)
        .where(*financial_conditions)
    )).one()
    failed_count = int((await db.execute(
        select(func.count(OperationFeeSnapshot.id))
        .select_from(OperationFeeSnapshot)
        .join(Deposit, Deposit.id == OperationFeeSnapshot.deposit_id)
        .where(*conditions, Deposit.status.in_(FAILED_OPERATION_STATUSES))
    )).scalar_one())

    total_count = int(all_aggregate[0])
    page = max(1, int(page))
    page_size = min(200, max(1, int(page_size)))
    result_rows = (await db.execute(
        base.order_by(OperationFeeSnapshot.rate_snapshot_at.desc(), OperationFeeSnapshot.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )).all()
    settled_operation_ids = (await db.execute(
        select(OperationFeeSnapshot.deposit_id)
        .join(Deposit, Deposit.id == OperationFeeSnapshot.deposit_id)
        .where(*financial_conditions)
    )).scalars().all()
    ledger_income = Decimal('0.00')
    if settled_operation_ids:
        ledger_income = money((await db.execute(
            select(func.coalesce(func.sum(PlatformLedgerEntry.amount), 0)).where(
                PlatformLedgerEntry.operation_id.in_(settled_operation_ids),
                PlatformLedgerEntry.entry_type == 'platform_income',
            )
        )).scalar_one())
    snapshot_income = money(financial_aggregate[4])
    expected_teamlead_expense = Decimal('0.00')
    expected_teamlead_expense_reversal = Decimal('0.00')
    teamlead_expense = Decimal('0.00')
    teamlead_expense_reversal = Decimal('0.00')
    if settled_operation_ids:
        accrual_rows = (await db.execute(
            select(
                TeamLeadAccrual.status,
                func.coalesce(func.sum(TeamLeadAccrual.accrual_rub), 0),
            ).where(
                TeamLeadAccrual.deposit_id.in_(settled_operation_ids),
            ).group_by(TeamLeadAccrual.status)
        )).all()
        accrual_totals = {row[0]: money(row[1]) for row in accrual_rows}
        merchant_accrual_rows = (await db.execute(
            select(
                TeamLeadMerchantAccrual.status,
                func.coalesce(func.sum(TeamLeadMerchantAccrual.accrual_rub), 0),
            ).where(
                TeamLeadMerchantAccrual.deposit_id.in_(settled_operation_ids),
            ).group_by(TeamLeadMerchantAccrual.status)
        )).all()
        merchant_accrual_totals = {
            row[0]: money(row[1]) for row in merchant_accrual_rows
        }
        expected_teamlead_expense = money(sum(
            (*accrual_totals.values(), *merchant_accrual_totals.values()),
            Decimal('0.00'),
        ))
        expected_teamlead_expense_reversal = money(
            accrual_totals.get('reversed', Decimal('0.00'))
            + merchant_accrual_totals.get('reversed', Decimal('0.00'))
        )
        expense_rows = (await db.execute(
            select(
                PlatformLedgerEntry.entry_type,
                func.coalesce(func.sum(PlatformLedgerEntry.amount), 0),
            ).where(
                PlatformLedgerEntry.operation_id.in_(settled_operation_ids),
                PlatformLedgerEntry.entry_type.in_(
                    ('teamlead_expense', 'teamlead_expense_reversal')
                ),
            ).group_by(PlatformLedgerEntry.entry_type)
        )).all()
        expense_totals = {row[0]: money(row[1]) for row in expense_rows}
        teamlead_expense = expense_totals.get(
            'teamlead_expense', Decimal('0.00')
        )
        teamlead_expense_reversal = expense_totals.get(
            'teamlead_expense_reversal', Decimal('0.00')
        )
    expected_net_teamlead_expense = money(
        expected_teamlead_expense - expected_teamlead_expense_reversal
    )
    net_teamlead_expense = money(teamlead_expense - teamlead_expense_reversal)
    teamlead_expense_gross_delta = money(
        expected_teamlead_expense - teamlead_expense
    )
    teamlead_expense_reversal_delta = money(
        expected_teamlead_expense_reversal - teamlead_expense_reversal
    )
    teamlead_expense_reconciliation_delta = money(
        expected_net_teamlead_expense - net_teamlead_expense
    )
    net_platform_income = money(snapshot_income - net_teamlead_expense)
    net_snapshot_platform_income = money(
        snapshot_income - expected_net_teamlead_expense
    )
    net_ledger_platform_income = money(ledger_income - net_teamlead_expense)
    gross_reconciliation_delta = money(snapshot_income - ledger_income)
    reconciliation_delta = money(
        net_snapshot_platform_income - net_ledger_platform_income
    )
    successful_turnover = money(financial_aggregate[1])
    average_margin_percent = Decimal('0.0000')
    if successful_turnover:
        average_margin_percent = (
            snapshot_income * Decimal('100') / successful_turnover
        ).quantize(Decimal('0.0001'))

    breakdown_rows = (await db.execute(
        select(
            OperationFeeSnapshot.payment_method,
            OperationFeeSnapshot.executor_type,
            func.count(OperationFeeSnapshot.id),
            func.coalesce(func.sum(OperationFeeSnapshot.calculation_base_amount), 0),
            func.coalesce(func.sum(OperationFeeSnapshot.platform_income_amount), 0),
        )
        .join(Deposit, Deposit.id == OperationFeeSnapshot.deposit_id)
        .where(*financial_conditions)
        .group_by(OperationFeeSnapshot.payment_method, OperationFeeSnapshot.executor_type)
        .order_by(OperationFeeSnapshot.payment_method, OperationFeeSnapshot.executor_type)
    )).all()
    merchant_breakdown_rows = (await db.execute(
        select(
            Deposit.merchant_id,
            Merchant.name,
            func.count(OperationFeeSnapshot.id),
            func.coalesce(func.sum(OperationFeeSnapshot.merchant_fee_amount), 0),
            func.coalesce(func.sum(OperationFeeSnapshot.platform_income_amount), 0),
        )
        .join(Deposit, Deposit.id == OperationFeeSnapshot.deposit_id)
        .join(Merchant, Merchant.id == Deposit.merchant_id)
        .where(*financial_conditions)
        .group_by(Deposit.merchant_id, Merchant.name)
        .order_by(func.sum(OperationFeeSnapshot.platform_income_amount).desc())
    )).all()
    executor_breakdown_rows = (await db.execute(
        select(
            OperationFeeSnapshot.executor_type,
            OperationFeeSnapshot.executor_id,
            func.count(OperationFeeSnapshot.id),
            func.coalesce(func.sum(OperationFeeSnapshot.executor_fee_amount), 0),
            func.coalesce(func.sum(OperationFeeSnapshot.platform_income_amount), 0),
        )
        .join(Deposit, Deposit.id == OperationFeeSnapshot.deposit_id)
        .where(*financial_conditions)
        .group_by(OperationFeeSnapshot.executor_type, OperationFeeSnapshot.executor_id)
        .order_by(OperationFeeSnapshot.executor_type, func.sum(OperationFeeSnapshot.platform_income_amount).desc())
    )).all()
    trader_executor_ids = {
        snapshot.executor_id
        for snapshot, _, _ in result_rows
        if snapshot.executor_type == 'trader'
    }
    trader_executor_ids.update(
        row[1] for row in executor_breakdown_rows if row[0] == 'trader'
    )
    aggregator_executor_ids = {
        snapshot.executor_id
        for snapshot, _, _ in result_rows
        if snapshot.executor_type == 'aggregator'
    }
    aggregator_executor_ids.update(
        row[1] for row in executor_breakdown_rows if row[0] == 'aggregator'
    )
    executor_labels: dict[tuple[str, UUID], str] = {}
    if trader_executor_ids:
        trader_rows = (await db.execute(
            select(User.id, User.email).where(User.id.in_(trader_executor_ids))
        )).all()
        executor_labels.update({('trader', row[0]): row[1] for row in trader_rows})
    if aggregator_executor_ids:
        aggregator_rows = (await db.execute(
            select(AggregatorAccount.id, AggregatorAccount.name).where(
                AggregatorAccount.id.in_(aggregator_executor_ids)
            )
        )).all()
        executor_labels.update({('aggregator', row[0]): row[1] for row in aggregator_rows})

    return {
        'filters': {
            'date_from': date_from,
            'date_to': date_to,
            'merchant_id': str(merchant_id) if merchant_id else '',
            'trader_id': str(trader_id) if trader_id else '',
            'aggregator_id': str(aggregator_id) if aggregator_id else '',
            'payment_method': payment_method or '',
            'currency': (currency or '').upper(),
            'executor_type': executor_type or '',
            'operation_status': operation_status or '',
            'settlement_status': settlement_status or '',
            'amount_min': str(amount_min) if amount_min is not None else '',
            'amount_max': str(amount_max) if amount_max is not None else '',
            'margin': margin or '',
        },
        'totals': {
            'operations_count': total_count,
            'turnover': money(all_aggregate[1]),
            'successful_turnover': successful_turnover,
            'merchant_fee': money(financial_aggregate[2]),
            'executor_fee': money(financial_aggregate[3]),
            'platform_income': snapshot_income,
            'expected_teamlead_expense': expected_teamlead_expense,
            'expected_teamlead_expense_reversal': (
                expected_teamlead_expense_reversal
            ),
            'expected_net_teamlead_expense': expected_net_teamlead_expense,
            'teamlead_expense': teamlead_expense,
            'teamlead_expense_reversal': teamlead_expense_reversal,
            'net_teamlead_expense': net_teamlead_expense,
            'teamlead_expense_gross_delta': teamlead_expense_gross_delta,
            'teamlead_expense_reversal_delta': (
                teamlead_expense_reversal_delta
            ),
            'teamlead_expense_reconciliation_delta': (
                teamlead_expense_reconciliation_delta
            ),
            'net_platform_income': net_platform_income,
            'net_snapshot_platform_income': net_snapshot_platform_income,
            'net_ledger_platform_income': net_ledger_platform_income,
            'loss_making_after_teamlead': net_platform_income < Decimal('0.00'),
            'average_platform_margin_percent': average_margin_percent,
            'successful_operations_count': int(financial_aggregate[0]),
            'failed_operations_count': failed_count,
            'ledger_platform_income': ledger_income,
            'gross_reconciliation_delta': gross_reconciliation_delta,
            'reconciliation_delta': reconciliation_delta,
            'reconciled': (
                gross_reconciliation_delta == Decimal('0.00')
                and teamlead_expense_gross_delta == Decimal('0.00')
                and teamlead_expense_reversal_delta == Decimal('0.00')
            ),
        },
        'rows': [
            {
                'snapshot': snapshot,
                'deposit': deposit,
                'merchant': merchant,
                'executor_name': executor_labels.get(
                    (snapshot.executor_type, snapshot.executor_id),
                    f'Archived {snapshot.executor_type}',
                ),
            }
            for snapshot, deposit, merchant in result_rows
        ],
        'breakdown': [
            {
                'payment_method': row[0],
                'executor_type': row[1],
                'operations_count': int(row[2]),
                'turnover': money(row[3]),
                'platform_income': money(row[4]),
            }
            for row in breakdown_rows
        ],
        'merchant_breakdown': [
            {
                'merchant_id': str(row[0]),
                'merchant_name': row[1] or 'Archived merchant',
                'operations_count': int(row[2]),
                'merchant_fee': money(row[3]),
                'platform_income': money(row[4]),
            }
            for row in merchant_breakdown_rows
        ],
        'executor_breakdown': [
            {
                'executor_type': row[0],
                'executor_id': str(row[1]),
                'executor_name': executor_labels.get((row[0], row[1]), f'Archived {row[0]}'),
                'operations_count': int(row[2]),
                'executor_fee': money(row[3]),
                'platform_income': money(row[4]),
            }
            for row in executor_breakdown_rows
        ],
        'page': page,
        'page_size': page_size,
        'total_pages': max(1, (total_count + page_size - 1) // page_size),
    }
