"""Add historical TeamLead merchant referrals.

Revision ID: 0025_teamlead_merchant_referrals
Revises: 0024_required_schema_indexes
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0025_teamlead_merchant_referrals'
down_revision = '0024_required_schema_indexes'
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
    op.create_table(
        'teamlead_merchant_assignments',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            'teamlead_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('users.id', ondelete='RESTRICT'),
            nullable=False,
        ),
        sa.Column(
            'merchant_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('merchants.id', ondelete='RESTRICT'),
            nullable=False,
        ),
        sa.Column('commission_percent', sa.Numeric(10, 6), nullable=False),
        sa.Column('valid_from', sa.DateTime(timezone=True), nullable=False),
        sa.Column('valid_to', sa.DateTime(timezone=True), nullable=True),
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
        sa.Column('reason', sa.String(length=500), nullable=False),
        sa.Column('close_reason', sa.String(length=500), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            'commission_percent >= 0 AND commission_percent <= 100',
            name='ck_teamlead_merchant_assignment_percent',
        ),
        sa.CheckConstraint(
            'valid_to IS NULL OR valid_to > valid_from',
            name='ck_teamlead_merchant_assignment_period',
        ),
    )
    for index_name, column in (
        ('ix_teamlead_merchant_assignments_teamlead_id', 'teamlead_id'),
        ('ix_teamlead_merchant_assignments_merchant_id', 'merchant_id'),
        ('ix_teamlead_merchant_assignments_valid_from', 'valid_from'),
        ('ix_teamlead_merchant_assignments_valid_to', 'valid_to'),
    ):
        op.create_index(index_name, 'teamlead_merchant_assignments', [column])
    op.create_index(
        'uq_teamlead_merchant_assignment_active_merchant',
        'teamlead_merchant_assignments',
        ['merchant_id'],
        unique=True,
        postgresql_where=sa.text('valid_to IS NULL'),
    )

    op.create_table(
        'teamlead_merchant_accruals',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            'teamlead_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('users.id', ondelete='RESTRICT'),
            nullable=False,
        ),
        sa.Column(
            'merchant_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('merchants.id', ondelete='RESTRICT'),
            nullable=False,
        ),
        sa.Column(
            'assignment_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                'teamlead_merchant_assignments.id',
                ondelete='RESTRICT',
            ),
            nullable=False,
        ),
        sa.Column(
            'deposit_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('deposits.id', ondelete='RESTRICT'),
            nullable=False,
        ),
        sa.Column(
            'source_type',
            sa.String(length=32),
            nullable=False,
            server_default='merchant_referral',
        ),
        sa.Column('gross_rub', sa.Numeric(18, 2), nullable=False),
        sa.Column(
            'commission_percent_snapshot',
            sa.Numeric(10, 6),
            nullable=False,
        ),
        sa.Column('accrual_rub', sa.Numeric(18, 2), nullable=False),
        sa.Column(
            'credited_to_available_rub',
            sa.Numeric(18, 2),
            nullable=False,
        ),
        sa.Column('applied_to_debt_rub', sa.Numeric(18, 2), nullable=False),
        sa.Column(
            'status',
            sa.String(length=16),
            nullable=False,
            server_default='credited',
        ),
        sa.Column('reversed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reversal_reason', sa.String(length=500), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint(
            'deposit_id',
            'source_type',
            name='uq_teamlead_merchant_accrual_deposit_source',
        ),
        sa.CheckConstraint(
            "source_type = 'merchant_referral'",
            name='ck_teamlead_merchant_accrual_source',
        ),
        sa.CheckConstraint(
            'gross_rub >= 0',
            name='ck_teamlead_merchant_accrual_gross',
        ),
        sa.CheckConstraint(
            'commission_percent_snapshot >= 0 '
            'AND commission_percent_snapshot <= 100',
            name='ck_teamlead_merchant_accrual_percent',
        ),
        sa.CheckConstraint(
            'accrual_rub >= 0',
            name='ck_teamlead_merchant_accrual_amount',
        ),
        sa.CheckConstraint(
            'credited_to_available_rub >= 0',
            name='ck_teamlead_merchant_accrual_credited',
        ),
        sa.CheckConstraint(
            'applied_to_debt_rub >= 0',
            name='ck_teamlead_merchant_accrual_debt_offset',
        ),
        sa.CheckConstraint(
            'credited_to_available_rub + applied_to_debt_rub = accrual_rub',
            name='ck_teamlead_merchant_accrual_distribution',
        ),
        sa.CheckConstraint(
            "status IN ('credited','reversed')",
            name='ck_teamlead_merchant_accrual_status',
        ),
    )
    for index_name, column in (
        ('ix_teamlead_merchant_accruals_teamlead_id', 'teamlead_id'),
        ('ix_teamlead_merchant_accruals_merchant_id', 'merchant_id'),
        ('ix_teamlead_merchant_accruals_assignment_id', 'assignment_id'),
        ('ix_teamlead_merchant_accruals_deposit_id', 'deposit_id'),
        ('ix_teamlead_merchant_accruals_status', 'status'),
    ):
        op.create_index(index_name, 'teamlead_merchant_accruals', [column])

    op.add_column(
        'teamlead_ledger_entries',
        sa.Column(
            'merchant_accrual_id',
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        'teamlead_ledger_entries',
        sa.Column(
            'merchant_id',
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        'teamlead_ledger_entries',
        sa.Column('source_type', sa.String(length=32), nullable=True),
    )
    op.create_foreign_key(
        'fk_teamlead_ledger_merchant_accrual',
        'teamlead_ledger_entries',
        'teamlead_merchant_accruals',
        ['merchant_accrual_id'],
        ['id'],
        ondelete='RESTRICT',
    )
    op.create_foreign_key(
        'fk_teamlead_ledger_merchant',
        'teamlead_ledger_entries',
        'merchants',
        ['merchant_id'],
        ['id'],
        ondelete='RESTRICT',
    )
    op.create_check_constraint(
        'ck_teamlead_ledger_source_type',
        'teamlead_ledger_entries',
        "source_type IS NULL OR "
        "source_type IN ('trader_referral','merchant_referral')",
    )
    for index_name, column in (
        (
            'ix_teamlead_ledger_entries_merchant_accrual_id',
            'merchant_accrual_id',
        ),
        ('ix_teamlead_ledger_entries_merchant_id', 'merchant_id'),
        ('ix_teamlead_ledger_entries_source_type', 'source_type'),
    ):
        op.create_index(index_name, 'teamlead_ledger_entries', [column])


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM teamlead_merchant_assignments)
               OR EXISTS (SELECT 1 FROM teamlead_merchant_accruals)
               OR EXISTS (
                    SELECT 1
                      FROM teamlead_ledger_entries
                     WHERE merchant_accrual_id IS NOT NULL
                        OR merchant_id IS NOT NULL
                        OR source_type IS NOT NULL
               )
            THEN
                RAISE EXCEPTION
                    'refusing TeamLead merchant referral downgrade after activity';
            END IF;
        END
        $$;
        """
    )
    for index_name in (
        'ix_teamlead_ledger_entries_source_type',
        'ix_teamlead_ledger_entries_merchant_id',
        'ix_teamlead_ledger_entries_merchant_accrual_id',
    ):
        op.drop_index(index_name, table_name='teamlead_ledger_entries')
    op.drop_constraint(
        'ck_teamlead_ledger_source_type',
        'teamlead_ledger_entries',
        type_='check',
    )
    op.drop_constraint(
        'fk_teamlead_ledger_merchant',
        'teamlead_ledger_entries',
        type_='foreignkey',
    )
    op.drop_constraint(
        'fk_teamlead_ledger_merchant_accrual',
        'teamlead_ledger_entries',
        type_='foreignkey',
    )
    op.drop_column('teamlead_ledger_entries', 'source_type')
    op.drop_column('teamlead_ledger_entries', 'merchant_id')
    op.drop_column('teamlead_ledger_entries', 'merchant_accrual_id')

    for index_name in (
        'ix_teamlead_merchant_accruals_status',
        'ix_teamlead_merchant_accruals_deposit_id',
        'ix_teamlead_merchant_accruals_assignment_id',
        'ix_teamlead_merchant_accruals_merchant_id',
        'ix_teamlead_merchant_accruals_teamlead_id',
    ):
        op.drop_index(index_name, table_name='teamlead_merchant_accruals')
    op.drop_table('teamlead_merchant_accruals')

    op.drop_index(
        'uq_teamlead_merchant_assignment_active_merchant',
        table_name='teamlead_merchant_assignments',
    )
    for index_name in (
        'ix_teamlead_merchant_assignments_valid_to',
        'ix_teamlead_merchant_assignments_valid_from',
        'ix_teamlead_merchant_assignments_merchant_id',
        'ix_teamlead_merchant_assignments_teamlead_id',
    ):
        op.drop_index(index_name, table_name='teamlead_merchant_assignments')
    op.drop_table('teamlead_merchant_assignments')
