"""Add Rolling accounts, allocations, and immutable ledger entries.

Revision ID: 0015_merchant_rolling
Revises: 0014_deposit_finance
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0015_merchant_rolling'
down_revision = '0014_deposit_finance'
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
        'merchant_rolling_accounts',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            'merchant_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('merchants.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column(
            'principal_usdt',
            sa.Numeric(24, 6),
            nullable=False,
            server_default='0',
        ),
        sa.Column(
            'recovered_usdt',
            sa.Numeric(24, 6),
            nullable=False,
            server_default='0',
        ),
        sa.Column(
            'outstanding_usdt',
            sa.Numeric(24, 6),
            nullable=False,
            server_default='0',
        ),
        sa.Column(
            'status',
            sa.String(length=16),
            nullable=False,
            server_default='exhausted',
        ),
        *_timestamps(),
        sa.UniqueConstraint(
            'merchant_id',
            name='uq_merchant_rolling_accounts_merchant_id',
        ),
        sa.CheckConstraint(
            'principal_usdt >= 0',
            name='ck_rolling_account_principal_nonnegative',
        ),
        sa.CheckConstraint(
            'recovered_usdt >= 0',
            name='ck_rolling_account_recovered_nonnegative',
        ),
        sa.CheckConstraint(
            'outstanding_usdt >= 0',
            name='ck_rolling_account_outstanding_nonnegative',
        ),
        sa.CheckConstraint(
            "status IN ('active','exhausted','suspended','closed')",
            name='ck_rolling_account_status',
        ),
    )
    op.create_index(
        'ix_merchant_rolling_accounts_status',
        'merchant_rolling_accounts',
        ['status'],
    )

    op.create_table(
        'merchant_rolling_allocations',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            'deposit_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('deposits.id', ondelete='RESTRICT'),
            nullable=False,
        ),
        sa.Column(
            'merchant_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('merchants.id', ondelete='RESTRICT'),
            nullable=False,
        ),
        sa.Column(
            'rolling_account_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('merchant_rolling_accounts.id', ondelete='RESTRICT'),
            nullable=False,
        ),
        sa.Column('gross_rub', sa.Numeric(18, 2), nullable=False),
        sa.Column(
            'merchant_fee_percent_snapshot',
            sa.Numeric(10, 6),
            nullable=False,
        ),
        sa.Column('merchant_fee_rub', sa.Numeric(18, 2), nullable=False),
        sa.Column('merchant_net_rub', sa.Numeric(18, 2), nullable=False),
        sa.Column('rapira_rate_rub', sa.Numeric(24, 8), nullable=False),
        sa.Column(
            'rapira_rate_side',
            sa.String(length=8),
            nullable=False,
            server_default='ask',
        ),
        sa.Column(
            'rapira_rate_source',
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            'rapira_rate_field',
            sa.String(length=64),
            nullable=False,
            server_default='askPrice',
        ),
        sa.Column(
            'rapira_rate_updated_at',
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column('merchant_net_usdt', sa.Numeric(24, 6), nullable=False),
        sa.Column(
            'rolling_applied_usdt',
            sa.Numeric(24, 6),
            nullable=False,
            server_default='0',
        ),
        sa.Column(
            'rolling_applied_rub',
            sa.Numeric(18, 2),
            nullable=False,
            server_default='0',
        ),
        sa.Column(
            'settle_credited_rub',
            sa.Numeric(18, 2),
            nullable=False,
            server_default='0',
        ),
        sa.Column(
            'status',
            sa.String(length=16),
            nullable=False,
            server_default='pending',
        ),
        sa.Column('release_reason', sa.String(length=64), nullable=True),
        sa.Column('finalized_at', sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint(
            'deposit_id',
            name='uq_merchant_rolling_allocations_deposit_id',
        ),
        sa.CheckConstraint(
            'gross_rub >= 0',
            name='ck_rolling_allocation_gross_nonnegative',
        ),
        sa.CheckConstraint(
            'merchant_fee_rub >= 0',
            name='ck_rolling_allocation_fee_nonnegative',
        ),
        sa.CheckConstraint(
            'merchant_net_rub >= 0',
            name='ck_rolling_allocation_net_rub_nonnegative',
        ),
        sa.CheckConstraint(
            'merchant_net_usdt >= 0',
            name='ck_rolling_allocation_net_usdt_nonnegative',
        ),
        sa.CheckConstraint(
            'rolling_applied_usdt >= 0',
            name='ck_rolling_allocation_applied_usdt_nonnegative',
        ),
        sa.CheckConstraint(
            'rolling_applied_rub >= 0',
            name='ck_rolling_allocation_applied_rub_nonnegative',
        ),
        sa.CheckConstraint(
            'settle_credited_rub >= 0',
            name='ck_rolling_allocation_settle_nonnegative',
        ),
        sa.CheckConstraint(
            "rapira_rate_side = 'ask'",
            name='ck_rolling_allocation_rate_side',
        ),
        sa.CheckConstraint(
            "status IN ('pending','paid','released','reversed')",
            name='ck_rolling_allocation_status',
        ),
    )
    op.create_index(
        'ix_merchant_rolling_allocations_merchant_id',
        'merchant_rolling_allocations',
        ['merchant_id'],
    )
    op.create_index(
        'ix_merchant_rolling_allocations_rolling_account_id',
        'merchant_rolling_allocations',
        ['rolling_account_id'],
    )
    op.create_index(
        'ix_merchant_rolling_allocations_status',
        'merchant_rolling_allocations',
        ['status'],
    )
    op.create_index(
        'ix_merchant_rolling_allocations_merchant_status',
        'merchant_rolling_allocations',
        ['merchant_id', 'status'],
    )

    op.create_table(
        'merchant_rolling_ledger_entries',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            'rolling_account_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('merchant_rolling_accounts.id', ondelete='RESTRICT'),
            nullable=False,
        ),
        sa.Column(
            'merchant_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('merchants.id', ondelete='RESTRICT'),
            nullable=False,
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
        sa.Column('amount_usdt', sa.Numeric(24, 6), nullable=True),
        sa.Column('amount_rub', sa.Numeric(18, 2), nullable=True),
        sa.Column('rate_rub', sa.Numeric(24, 8), nullable=True),
        sa.Column('network', sa.String(length=32), nullable=True),
        sa.Column('destination_address', sa.String(length=160), nullable=True),
        sa.Column('tx_hash', sa.String(length=160), nullable=True),
        sa.Column('funded_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reason', sa.String(length=500), nullable=False),
        sa.Column('idempotency_key', sa.String(length=180), nullable=False),
        sa.Column(
            'metadata_json',
            postgresql.JSON(),
            nullable=False,
            server_default='{}',
        ),
        *_timestamps(),
        sa.UniqueConstraint(
            'idempotency_key',
            name='uq_merchant_rolling_ledger_idempotency_key',
        ),
        sa.CheckConstraint(
            "entry_type IN ('funding','topup','pending_added','pending_released',"
            "'recovery','settle_overflow','reversal','manual_adjustment','write_off')",
            name='ck_rolling_ledger_entry_type',
        ),
        sa.CheckConstraint(
            'amount_usdt IS NULL OR amount_usdt >= 0',
            name='ck_rolling_ledger_amount_usdt_nonnegative',
        ),
        sa.CheckConstraint(
            'amount_rub IS NULL OR amount_rub >= 0',
            name='ck_rolling_ledger_amount_rub_nonnegative',
        ),
    )
    op.create_index(
        'ix_merchant_rolling_ledger_entries_rolling_account_id',
        'merchant_rolling_ledger_entries',
        ['rolling_account_id'],
    )
    op.create_index(
        'ix_merchant_rolling_ledger_entries_merchant_id',
        'merchant_rolling_ledger_entries',
        ['merchant_id'],
    )
    op.create_index(
        'ix_merchant_rolling_ledger_entries_deposit_id',
        'merchant_rolling_ledger_entries',
        ['deposit_id'],
    )
    op.create_index(
        'ix_merchant_rolling_ledger_entries_actor_id',
        'merchant_rolling_ledger_entries',
        ['actor_id'],
    )
    op.create_index(
        'ix_merchant_rolling_ledger_entries_entry_type',
        'merchant_rolling_ledger_entries',
        ['entry_type'],
    )
    op.create_index(
        'ix_merchant_rolling_ledger_account_created',
        'merchant_rolling_ledger_entries',
        ['rolling_account_id', 'created_at'],
    )
    op.create_index(
        'uq_merchant_rolling_ledger_network_tx_hash',
        'merchant_rolling_ledger_entries',
        ['network', 'tx_hash'],
        unique=True,
        postgresql_where=sa.text(
            "entry_type IN ('funding','topup') "
            "AND network IS NOT NULL AND tx_hash IS NOT NULL"
        ),
    )
    op.execute(
        """
        CREATE FUNCTION protect_rolling_ledger_immutable()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'rolling ledger entries are immutable';
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_rolling_ledger_immutable
        BEFORE UPDATE OR DELETE ON merchant_rolling_ledger_entries
        FOR EACH ROW EXECUTE FUNCTION protect_rolling_ledger_immutable();
        """
    )


def downgrade() -> None:
    # A funded or traffic-bearing Rolling ledger is financial evidence and must
    # never be destroyed by an automated rollback.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                  FROM merchant_rolling_ledger_entries
                 WHERE entry_type IN (
                    'funding',
                    'topup',
                    'pending_added',
                    'pending_released',
                    'recovery',
                    'settle_overflow',
                    'reversal',
                    'manual_adjustment',
                    'write_off'
                 )
            ) OR EXISTS (
                SELECT 1
                  FROM merchant_rolling_allocations
            ) OR EXISTS (
                SELECT 1
                  FROM merchant_rolling_accounts
                 WHERE principal_usdt <> 0
                    OR recovered_usdt <> 0
                    OR outstanding_usdt <> 0
            ) THEN
                RAISE EXCEPTION
                    'refusing destructive Rolling downgrade after funding or traffic';
            END IF;
        END
        $$;
        """
    )
    op.execute(
        'DROP TRIGGER IF EXISTS trg_rolling_ledger_immutable '
        'ON merchant_rolling_ledger_entries'
    )
    op.execute('DROP FUNCTION IF EXISTS protect_rolling_ledger_immutable()')
    op.drop_index(
        'uq_merchant_rolling_ledger_network_tx_hash',
        table_name='merchant_rolling_ledger_entries',
    )
    op.drop_index(
        'ix_merchant_rolling_ledger_account_created',
        table_name='merchant_rolling_ledger_entries',
    )
    for name in (
        'ix_merchant_rolling_ledger_entries_entry_type',
        'ix_merchant_rolling_ledger_entries_actor_id',
        'ix_merchant_rolling_ledger_entries_deposit_id',
        'ix_merchant_rolling_ledger_entries_merchant_id',
        'ix_merchant_rolling_ledger_entries_rolling_account_id',
    ):
        op.drop_index(name, table_name='merchant_rolling_ledger_entries')
    op.drop_table('merchant_rolling_ledger_entries')

    for name in (
        'ix_merchant_rolling_allocations_merchant_status',
        'ix_merchant_rolling_allocations_status',
        'ix_merchant_rolling_allocations_rolling_account_id',
        'ix_merchant_rolling_allocations_merchant_id',
    ):
        op.drop_index(name, table_name='merchant_rolling_allocations')
    op.drop_table('merchant_rolling_allocations')

    op.drop_index(
        'ix_merchant_rolling_accounts_status',
        table_name='merchant_rolling_accounts',
    )
    op.drop_table('merchant_rolling_accounts')
