"""fee tiers, immutable operation snapshots, and tenant archival

Revision ID: 0010_fee_tiers
Revises: 0009_requisite_provider_codes
Create Date: 2026-07-15
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0010_fee_tiers'
down_revision = '0009_requisite_provider_codes'
branch_labels = None
depends_on = None


ARCHIVABLE_TABLES = ('users', 'merchants', 'requisites', 'aggregator_accounts', 'aggregator_merchants')


def upgrade():
    for table in ARCHIVABLE_TABLES:
        op.add_column(table, sa.Column('is_archived', sa.Boolean(), nullable=False, server_default=sa.false()))
        op.add_column(table, sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True))
        op.create_index(f'ix_{table}_is_archived', table, ['is_archived'])
    op.add_column('users', sa.Column('archived_reason', sa.String(length=255), nullable=True))

    op.add_column('fee_rules', sa.Column('entity_type', sa.String(length=32), nullable=False, server_default='merchant'))
    op.add_column('fee_rules', sa.Column('entity_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column('fee_rules', sa.Column('fee_side', sa.String(length=32), nullable=False, server_default='merchant_fee'))
    op.add_column('fee_rules', sa.Column('payment_method', sa.String(length=32), nullable=False, server_default='sbp'))
    op.add_column('fee_rules', sa.Column('currency', sa.String(length=8), nullable=False, server_default='RUB'))
    op.add_column('fee_rules', sa.Column('min_amount', sa.Numeric(18, 2), nullable=False, server_default='0.00'))
    op.add_column('fee_rules', sa.Column('max_amount', sa.Numeric(18, 2), nullable=True))
    op.add_column('fee_rules', sa.Column('rate_percent', sa.Numeric(8, 4), nullable=False, server_default='0.0000'))
    op.add_column('fee_rules', sa.Column('effective_from', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.add_column('fee_rules', sa.Column('effective_to', sa.DateTime(timezone=True), nullable=True))
    op.add_column('fee_rules', sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column('fee_rules', sa.Column('version', sa.Integer(), nullable=False, server_default='1'))
    op.add_column('fee_rules', sa.Column('created_by', postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column('fee_rules', sa.Column('updated_by', postgresql.UUID(as_uuid=True), nullable=True))
    op.execute(
        """
        UPDATE fee_rules
           SET entity_id = merchant_id,
               payment_method = method,
               rate_percent = percent,
               effective_from = created_at
        """
    )
    op.create_index('ix_fee_rules_entity_type', 'fee_rules', ['entity_type'])
    op.create_index('ix_fee_rules_entity_id', 'fee_rules', ['entity_id'])
    op.create_index('ix_fee_rules_fee_side', 'fee_rules', ['fee_side'])
    op.create_index('ix_fee_rules_payment_method', 'fee_rules', ['payment_method'])
    op.create_index('ix_fee_rules_currency', 'fee_rules', ['currency'])
    op.create_index('ix_fee_rules_is_active', 'fee_rules', ['is_active'])
    op.create_index(
        'ix_fee_rules_lookup', 'fee_rules',
        ['entity_type', 'entity_id', 'fee_side', 'payment_method', 'currency', 'is_active', 'effective_from'],
    )
    op.create_check_constraint('ck_fee_rules_min_amount_nonnegative', 'fee_rules', 'min_amount >= 0')
    op.create_check_constraint('ck_fee_rules_amount_range', 'fee_rules', 'max_amount IS NULL OR max_amount > min_amount')
    op.create_check_constraint('ck_fee_rules_rate_percent', 'fee_rules', 'rate_percent >= 0 AND rate_percent <= 100')
    op.create_check_constraint('ck_fee_rules_effective_period', 'fee_rules', 'effective_to IS NULL OR effective_to > effective_from')

    op.create_table(
        'operation_fee_snapshots',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('deposit_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('merchant_rate_rule_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('merchant_rate_version', sa.Integer(), nullable=False),
        sa.Column('merchant_rate_percent', sa.Numeric(8, 4), nullable=False),
        sa.Column('merchant_fee_amount', sa.Numeric(18, 2), nullable=False),
        sa.Column('executor_type', sa.String(length=32), nullable=False),
        sa.Column('executor_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('executor_rate_rule_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('executor_rate_version', sa.Integer(), nullable=False),
        sa.Column('executor_rate_percent', sa.Numeric(8, 4), nullable=False),
        sa.Column('executor_fee_amount', sa.Numeric(18, 2), nullable=False),
        sa.Column('platform_margin_percent', sa.Numeric(8, 4), nullable=False),
        sa.Column('platform_income_amount', sa.Numeric(18, 2), nullable=False),
        sa.Column('calculation_base_amount', sa.Numeric(18, 2), nullable=False),
        sa.Column('currency', sa.String(length=8), nullable=False),
        sa.Column('payment_method', sa.String(length=32), nullable=False),
        sa.Column('rate_snapshot_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('settlement_status', sa.String(length=32), nullable=False, server_default='pending'),
        sa.Column('settled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ledger_reference', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['deposit_id'], ['deposits.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['merchant_rate_rule_id'], ['fee_rules.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['executor_rate_rule_id'], ['fee_rules.id'], ondelete='SET NULL'),
        sa.UniqueConstraint('deposit_id', name='uq_operation_fee_snapshots_deposit_id'),
        sa.CheckConstraint('merchant_rate_percent >= executor_rate_percent', name='ck_operation_snapshot_nonnegative_margin'),
        sa.CheckConstraint('merchant_fee_amount = executor_fee_amount + platform_income_amount', name='ck_operation_snapshot_fee_invariant'),
        sa.CheckConstraint("executor_type IN ('trader','aggregator')", name='ck_operation_snapshot_executor_type'),
    )
    op.create_index('ix_operation_fee_snapshots_deposit_id', 'operation_fee_snapshots', ['deposit_id'], unique=True)
    op.create_index('ix_operation_fee_snapshots_executor_type', 'operation_fee_snapshots', ['executor_type'])
    op.create_index('ix_operation_fee_snapshots_executor_id', 'operation_fee_snapshots', ['executor_id'])
    op.create_index('ix_operation_fee_snapshots_currency', 'operation_fee_snapshots', ['currency'])
    op.create_index('ix_operation_fee_snapshots_payment_method', 'operation_fee_snapshots', ['payment_method'])
    op.create_index('ix_operation_fee_snapshots_rate_snapshot_at', 'operation_fee_snapshots', ['rate_snapshot_at'])
    op.create_index('ix_operation_fee_snapshots_settlement_status', 'operation_fee_snapshots', ['settlement_status'])
    op.execute(
        """
        CREATE FUNCTION protect_operation_fee_snapshot_immutable_fields()
        RETURNS trigger AS $$
        BEGIN
            IF (to_jsonb(NEW) - ARRAY['settlement_status', 'settled_at', 'ledger_reference', 'updated_at'])
               IS DISTINCT FROM
               (to_jsonb(OLD) - ARRAY['settlement_status', 'settled_at', 'ledger_reference', 'updated_at'])
            THEN
                RAISE EXCEPTION 'operation fee snapshot financial fields are immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER trg_operation_fee_snapshot_immutable
        BEFORE UPDATE ON operation_fee_snapshots
        FOR EACH ROW EXECUTE FUNCTION protect_operation_fee_snapshot_immutable_fields();
        """
    )


def downgrade():
    op.execute('DROP TRIGGER IF EXISTS trg_operation_fee_snapshot_immutable ON operation_fee_snapshots')
    op.execute('DROP FUNCTION IF EXISTS protect_operation_fee_snapshot_immutable_fields()')
    for name in (
        'ix_operation_fee_snapshots_settlement_status',
        'ix_operation_fee_snapshots_rate_snapshot_at',
        'ix_operation_fee_snapshots_payment_method',
        'ix_operation_fee_snapshots_currency',
        'ix_operation_fee_snapshots_executor_id',
        'ix_operation_fee_snapshots_executor_type',
        'ix_operation_fee_snapshots_deposit_id',
    ):
        op.drop_index(name, table_name='operation_fee_snapshots')
    op.drop_table('operation_fee_snapshots')

    for name in (
        'ck_fee_rules_effective_period',
        'ck_fee_rules_rate_percent',
        'ck_fee_rules_amount_range',
        'ck_fee_rules_min_amount_nonnegative',
    ):
        op.drop_constraint(name, 'fee_rules', type_='check')
    for name in (
        'ix_fee_rules_lookup', 'ix_fee_rules_is_active', 'ix_fee_rules_currency',
        'ix_fee_rules_payment_method', 'ix_fee_rules_fee_side', 'ix_fee_rules_entity_id',
        'ix_fee_rules_entity_type',
    ):
        op.drop_index(name, table_name='fee_rules')
    for column in (
        'updated_by', 'created_by', 'version', 'is_active', 'effective_to', 'effective_from',
        'rate_percent', 'max_amount', 'min_amount', 'currency', 'payment_method', 'fee_side',
        'entity_id', 'entity_type',
    ):
        op.drop_column('fee_rules', column)

    op.drop_column('users', 'archived_reason')
    for table in reversed(ARCHIVABLE_TABLES):
        op.drop_index(f'ix_{table}_is_archived', table_name=table)
        op.drop_column(table, 'archived_at')
        op.drop_column(table, 'is_archived')
