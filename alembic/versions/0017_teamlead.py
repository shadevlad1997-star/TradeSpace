"""Add historical TeamLead assignments and financial accounting.

Revision ID: 0017_teamlead
Revises: 0016_rolling_rapira_freshness
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0017_teamlead'
down_revision = '0016_rolling_rapira_freshness'
branch_labels = None
depends_on = None


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def upgrade() -> None:
    # users.role is a constrained application string, not a PostgreSQL enum.
    # Adding Role.teamlead therefore needs no existing-user rewrite.
    op.create_table(
        'teamlead_trader_assignments',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            'teamlead_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('users.id', ondelete='RESTRICT'),
            nullable=False,
        ),
        sa.Column(
            'trader_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('users.id', ondelete='RESTRICT'),
            nullable=False,
        ),
        sa.Column('commission_percent', sa.Numeric(10, 6), nullable=False),
        sa.Column('effective_from', sa.DateTime(timezone=True), nullable=False),
        sa.Column('effective_to', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_by',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('users.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column(
            'closed_by',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('users.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column('creation_reason', sa.String(length=500), nullable=False),
        sa.Column('close_reason', sa.String(length=500), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            'commission_percent >= 0 AND commission_percent <= 100',
            name='ck_teamlead_assignment_percent',
        ),
        sa.CheckConstraint(
            'effective_to IS NULL OR effective_to > effective_from',
            name='ck_teamlead_assignment_effective_period',
        ),
    )
    op.create_index(
        'ix_teamlead_trader_assignments_teamlead_id',
        'teamlead_trader_assignments',
        ['teamlead_id'],
    )
    op.create_index(
        'ix_teamlead_trader_assignments_trader_id',
        'teamlead_trader_assignments',
        ['trader_id'],
    )
    op.create_index(
        'ix_teamlead_trader_assignments_effective_from',
        'teamlead_trader_assignments',
        ['effective_from'],
    )
    op.create_index(
        'ix_teamlead_trader_assignments_effective_to',
        'teamlead_trader_assignments',
        ['effective_to'],
    )
    op.create_index(
        'uq_teamlead_assignment_active_trader',
        'teamlead_trader_assignments',
        ['trader_id'],
        unique=True,
        postgresql_where=sa.text('effective_to IS NULL'),
    )

    op.create_table(
        'teamlead_balances',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            'teamlead_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('users.id', ondelete='RESTRICT'),
            nullable=False,
        ),
        sa.Column('available_rub', sa.Numeric(18, 2), nullable=False, server_default='0'),
        sa.Column('frozen_rub', sa.Numeric(18, 2), nullable=False, server_default='0'),
        sa.Column('debt_rub', sa.Numeric(18, 2), nullable=False, server_default='0'),
        sa.Column('total_earned_rub', sa.Numeric(18, 2), nullable=False, server_default='0'),
        sa.Column('total_paid_rub', sa.Numeric(18, 2), nullable=False, server_default='0'),
        *_timestamps(),
        sa.UniqueConstraint('teamlead_id', name='uq_teamlead_balances_teamlead_id'),
        sa.CheckConstraint('available_rub >= 0', name='ck_teamlead_balance_available'),
        sa.CheckConstraint('frozen_rub >= 0', name='ck_teamlead_balance_frozen'),
        sa.CheckConstraint('debt_rub >= 0', name='ck_teamlead_balance_debt'),
        sa.CheckConstraint('total_earned_rub >= 0', name='ck_teamlead_balance_earned'),
        sa.CheckConstraint('total_paid_rub >= 0', name='ck_teamlead_balance_paid'),
    )
    op.create_index(
        'ix_teamlead_balances_teamlead_id',
        'teamlead_balances',
        ['teamlead_id'],
    )

    op.create_table(
        'teamlead_accruals',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            'teamlead_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('users.id', ondelete='RESTRICT'),
            nullable=False,
        ),
        sa.Column(
            'trader_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('users.id', ondelete='RESTRICT'),
            nullable=False,
        ),
        sa.Column(
            'assignment_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('teamlead_trader_assignments.id', ondelete='RESTRICT'),
            nullable=False,
        ),
        sa.Column(
            'deposit_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('deposits.id', ondelete='RESTRICT'),
            nullable=False,
        ),
        sa.Column('gross_rub', sa.Numeric(18, 2), nullable=False),
        sa.Column('commission_percent_snapshot', sa.Numeric(10, 6), nullable=False),
        sa.Column('accrual_rub', sa.Numeric(18, 2), nullable=False),
        sa.Column('credited_to_available_rub', sa.Numeric(18, 2), nullable=False),
        sa.Column('applied_to_debt_rub', sa.Numeric(18, 2), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='credited'),
        sa.Column('reversed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reversal_reason', sa.String(length=500), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint('deposit_id', name='uq_teamlead_accruals_deposit_id'),
        sa.CheckConstraint('gross_rub >= 0', name='ck_teamlead_accrual_gross'),
        sa.CheckConstraint(
            'commission_percent_snapshot >= 0 AND commission_percent_snapshot <= 100',
            name='ck_teamlead_accrual_percent',
        ),
        sa.CheckConstraint('accrual_rub >= 0', name='ck_teamlead_accrual_amount'),
        sa.CheckConstraint('credited_to_available_rub >= 0', name='ck_teamlead_accrual_credited'),
        sa.CheckConstraint('applied_to_debt_rub >= 0', name='ck_teamlead_accrual_debt_offset'),
        sa.CheckConstraint(
            'credited_to_available_rub + applied_to_debt_rub = accrual_rub',
            name='ck_teamlead_accrual_distribution',
        ),
        sa.CheckConstraint(
            "status IN ('credited','reversed')",
            name='ck_teamlead_accrual_status',
        ),
    )
    for index_name, column in (
        ('ix_teamlead_accruals_teamlead_id', 'teamlead_id'),
        ('ix_teamlead_accruals_trader_id', 'trader_id'),
        ('ix_teamlead_accruals_assignment_id', 'assignment_id'),
        ('ix_teamlead_accruals_deposit_id', 'deposit_id'),
        ('ix_teamlead_accruals_status', 'status'),
    ):
        op.create_index(index_name, 'teamlead_accruals', [column])

    op.create_table(
        'teamlead_settlements',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            'teamlead_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('users.id', ondelete='RESTRICT'),
            nullable=False,
        ),
        sa.Column('requested_usdt', sa.Numeric(24, 6), nullable=False),
        sa.Column('fee_usdt', sa.Numeric(24, 6), nullable=False, server_default='5'),
        sa.Column('total_debit_usdt', sa.Numeric(24, 6), nullable=False),
        sa.Column('rapira_rate_rub', sa.Numeric(24, 8), nullable=False),
        sa.Column('rate_symbol', sa.String(length=16), nullable=False),
        sa.Column('rate_source', sa.String(length=64), nullable=False),
        sa.Column('rate_side', sa.String(length=8), nullable=False, server_default='ask'),
        sa.Column('provider_timestamp', sa.DateTime(timezone=True), nullable=True),
        sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('freshness_basis', sa.String(length=32), nullable=False),
        sa.Column('requested_rub', sa.Numeric(18, 2), nullable=False),
        sa.Column('fee_rub', sa.Numeric(18, 2), nullable=False),
        sa.Column('total_debit_rub', sa.Numeric(18, 2), nullable=False),
        sa.Column('network', sa.String(length=16), nullable=False, server_default='TRC20'),
        sa.Column('wallet_address', sa.String(length=128), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='pending'),
        sa.Column('idempotency_key', sa.String(length=180), nullable=False),
        sa.Column('requested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('rejected_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'processed_by',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('users.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column('tx_hash', sa.String(length=160), nullable=True),
        sa.Column('reject_reason', sa.String(length=500), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint('idempotency_key', name='uq_teamlead_settlement_idempotency'),
        sa.CheckConstraint('requested_usdt > 0', name='ck_teamlead_settlement_requested'),
        sa.CheckConstraint('fee_usdt = 5', name='ck_teamlead_settlement_fee'),
        sa.CheckConstraint(
            'total_debit_usdt = requested_usdt + fee_usdt',
            name='ck_teamlead_settlement_total_usdt',
        ),
        sa.CheckConstraint(
            'total_debit_rub = requested_rub + fee_rub',
            name='ck_teamlead_settlement_total_rub',
        ),
        sa.CheckConstraint('rapira_rate_rub > 0', name='ck_teamlead_settlement_rate'),
        sa.CheckConstraint("rate_side = 'ask'", name='ck_teamlead_settlement_side'),
        sa.CheckConstraint(
            "rate_source = 'rapira_live'",
            name='ck_teamlead_settlement_source',
        ),
        sa.CheckConstraint(
            "freshness_basis IN ('provider_timestamp','fetched_at')",
            name='ck_teamlead_settlement_freshness',
        ),
        sa.CheckConstraint("network = 'TRC20'", name='ck_teamlead_settlement_network'),
        sa.CheckConstraint(
            "status IN ('pending','completed','rejected')",
            name='ck_teamlead_settlement_status',
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND completed_at IS NULL "
            "AND rejected_at IS NULL AND tx_hash IS NULL "
            "AND reject_reason IS NULL) OR "
            "(status = 'completed' AND completed_at IS NOT NULL "
            "AND rejected_at IS NULL AND tx_hash IS NOT NULL "
            "AND btrim(tx_hash) <> '' AND reject_reason IS NULL) OR "
            "(status = 'rejected' AND rejected_at IS NOT NULL "
            "AND completed_at IS NULL AND tx_hash IS NULL "
            "AND reject_reason IS NOT NULL AND btrim(reject_reason) <> '')",
            name='ck_teamlead_settlement_state',
        ),
    )
    op.create_index(
        'ix_teamlead_settlements_teamlead_id',
        'teamlead_settlements',
        ['teamlead_id'],
    )
    op.create_index(
        'ix_teamlead_settlements_status',
        'teamlead_settlements',
        ['status'],
    )
    op.create_index(
        'uq_teamlead_settlement_pending',
        'teamlead_settlements',
        ['teamlead_id'],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        'uq_teamlead_settlement_completed_tx',
        'teamlead_settlements',
        ['network', 'tx_hash'],
        unique=True,
        postgresql_where=sa.text(
            "status = 'completed' AND tx_hash IS NOT NULL"
        ),
    )

    op.create_table(
        'teamlead_ledger_entries',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            'teamlead_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('users.id', ondelete='RESTRICT'),
            nullable=False,
        ),
        sa.Column(
            'accrual_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('teamlead_accruals.id', ondelete='RESTRICT'),
            nullable=True,
        ),
        sa.Column(
            'settlement_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('teamlead_settlements.id', ondelete='RESTRICT'),
            nullable=True,
        ),
        sa.Column(
            'deposit_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('deposits.id', ondelete='RESTRICT'),
            nullable=True,
        ),
        sa.Column(
            'actor_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('users.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column('entry_type', sa.String(length=32), nullable=False),
        sa.Column('amount_rub', sa.Numeric(18, 2), nullable=False),
        sa.Column('available_after', sa.Numeric(18, 2), nullable=False),
        sa.Column('frozen_after', sa.Numeric(18, 2), nullable=False),
        sa.Column('debt_after', sa.Numeric(18, 2), nullable=False),
        sa.Column('idempotency_key', sa.String(length=180), nullable=False),
        sa.Column('reason', sa.String(length=500), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint('idempotency_key', name='uq_teamlead_ledger_idempotency'),
        sa.CheckConstraint('amount_rub >= 0', name='ck_teamlead_ledger_amount'),
        sa.CheckConstraint('available_after >= 0', name='ck_teamlead_ledger_available'),
        sa.CheckConstraint('frozen_after >= 0', name='ck_teamlead_ledger_frozen'),
        sa.CheckConstraint('debt_after >= 0', name='ck_teamlead_ledger_debt'),
        sa.CheckConstraint(
            "entry_type IN ('accrual_credit','debt_offset','accrual_reversal',"
            "'settlement_freeze','settlement_release','settlement_complete',"
            "'manual_adjustment','write_off')",
            name='ck_teamlead_ledger_type',
        ),
    )
    for index_name, column in (
        ('ix_teamlead_ledger_entries_teamlead_id', 'teamlead_id'),
        ('ix_teamlead_ledger_entries_entry_type', 'entry_type'),
    ):
        op.create_index(index_name, 'teamlead_ledger_entries', [column])
    op.create_index(
        'ix_teamlead_ledger_teamlead_created',
        'teamlead_ledger_entries',
        ['teamlead_id', 'created_at'],
    )
    op.execute(
        """
        CREATE FUNCTION protect_teamlead_ledger_immutable()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'teamlead ledger entries are immutable';
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_teamlead_ledger_immutable
        BEFORE UPDATE OR DELETE ON teamlead_ledger_entries
        FOR EACH ROW EXECUTE FUNCTION protect_teamlead_ledger_immutable();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM teamlead_accruals)
               OR EXISTS (SELECT 1 FROM teamlead_settlements)
               OR EXISTS (SELECT 1 FROM teamlead_ledger_entries)
               OR EXISTS (
                    SELECT 1
                      FROM teamlead_balances
                     WHERE available_rub <> 0
                        OR frozen_rub <> 0
                        OR debt_rub <> 0
                        OR total_earned_rub <> 0
                        OR total_paid_rub <> 0
               )
            THEN
                RAISE EXCEPTION
                    'refusing destructive TeamLead downgrade after financial activity';
            END IF;
        END
        $$;
        """
    )
    op.execute(
        'DROP TRIGGER IF EXISTS trg_teamlead_ledger_immutable '
        'ON teamlead_ledger_entries'
    )
    op.execute('DROP FUNCTION IF EXISTS protect_teamlead_ledger_immutable()')
    op.drop_index('ix_teamlead_ledger_teamlead_created', table_name='teamlead_ledger_entries')
    op.drop_index('ix_teamlead_ledger_entries_entry_type', table_name='teamlead_ledger_entries')
    op.drop_index('ix_teamlead_ledger_entries_teamlead_id', table_name='teamlead_ledger_entries')
    op.drop_table('teamlead_ledger_entries')

    op.drop_index('uq_teamlead_settlement_pending', table_name='teamlead_settlements')
    op.drop_index(
        'uq_teamlead_settlement_completed_tx',
        table_name='teamlead_settlements',
    )
    op.drop_index('ix_teamlead_settlements_status', table_name='teamlead_settlements')
    op.drop_index('ix_teamlead_settlements_teamlead_id', table_name='teamlead_settlements')
    op.drop_table('teamlead_settlements')

    for index_name in (
        'ix_teamlead_accruals_status',
        'ix_teamlead_accruals_deposit_id',
        'ix_teamlead_accruals_assignment_id',
        'ix_teamlead_accruals_trader_id',
        'ix_teamlead_accruals_teamlead_id',
    ):
        op.drop_index(index_name, table_name='teamlead_accruals')
    op.drop_table('teamlead_accruals')

    op.drop_index('ix_teamlead_balances_teamlead_id', table_name='teamlead_balances')
    op.drop_table('teamlead_balances')

    op.drop_index(
        'uq_teamlead_assignment_active_trader',
        table_name='teamlead_trader_assignments',
    )
    for index_name in (
        'ix_teamlead_trader_assignments_effective_to',
        'ix_teamlead_trader_assignments_effective_from',
        'ix_teamlead_trader_assignments_trader_id',
        'ix_teamlead_trader_assignments_teamlead_id',
    ):
        op.drop_index(index_name, table_name='teamlead_trader_assignments')
    op.drop_table('teamlead_trader_assignments')
