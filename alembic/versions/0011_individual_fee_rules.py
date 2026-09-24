"""Require entity-scoped fee rules and snapshot entity references.

Revision ID: 0011_individual_fee_rules
Revises: 0010_fee_tiers
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0011_individual_fee_rules'
down_revision = '0010_fee_tiers'
branch_labels = None
depends_on = None


ZERO_UUID = '00000000-0000-0000-0000-000000000000'


def upgrade():
    bind = op.get_bind()
    referenced_generic = bind.execute(sa.text(
        """
        SELECT count(*)
          FROM fee_rules fr
         WHERE (fr.entity_id IS NULL OR fr.entity_id = CAST(:zero_uuid AS uuid))
           AND EXISTS (
               SELECT 1
                 FROM operation_fee_snapshots snapshot
                WHERE snapshot.merchant_rate_rule_id = fr.id
                   OR snapshot.executor_rate_rule_id = fr.id
           )
        """
    ), {'zero_uuid': ZERO_UUID}).scalar_one()
    if referenced_generic:
        raise RuntimeError(
            'referenced generic fee rules require explicit manual entity remediation; '
            'migration refused to assign them to arbitrary entities'
        )

    op.execute(sa.text(
        """
        DELETE FROM fee_rules
         WHERE (entity_id IS NULL OR entity_id = CAST(:zero_uuid AS uuid))
           AND NOT EXISTS (
               SELECT 1
                 FROM operation_fee_snapshots snapshot
                WHERE snapshot.merchant_rate_rule_id = fee_rules.id
                   OR snapshot.executor_rate_rule_id = fee_rules.id
           )
        """
    ).bindparams(zero_uuid=ZERO_UUID))

    op.alter_column('fee_rules', 'entity_id', existing_type=postgresql.UUID(as_uuid=True), nullable=False)
    op.create_check_constraint(
        'ck_fee_rules_entity_type',
        'fee_rules',
        "entity_type IN ('merchant','trader','aggregator')",
    )
    op.create_check_constraint(
        'ck_fee_rules_entity_id_nonzero',
        'fee_rules',
        f"entity_id <> '{ZERO_UUID}'::uuid",
    )
    op.create_check_constraint(
        'ck_fee_rules_entity_fee_side',
        'fee_rules',
        "(entity_type = 'merchant' AND fee_side = 'merchant_fee') OR "
        "(entity_type IN ('trader','aggregator') AND fee_side = 'executor_fee')",
    )
    op.create_index('ix_fee_rules_entity_scope', 'fee_rules', ['entity_type', 'entity_id'])
    op.create_index(
        'uq_fee_rules_active_exact',
        'fee_rules',
        [
            'entity_type', 'entity_id', 'fee_side', 'payment_method', 'currency',
            'min_amount', 'max_amount', 'effective_from', 'effective_to',
        ],
        unique=True,
        postgresql_where=sa.text('is_active'),
        postgresql_nulls_not_distinct=True,
    )

    op.add_column(
        'operation_fee_snapshots',
        sa.Column('merchant_id', postgresql.UUID(as_uuid=True), nullable=True),
    )
    # Revision 0010 intentionally rejects every financial-field UPDATE.  The
    # new column must be backfilled once from the snapshot's immutable deposit
    # relationship, so suspend only that trigger for this deterministic UPDATE.
    # PostgreSQL transactional DDL guarantees a failed migration restores the
    # original trigger state.
    op.execute(
        'ALTER TABLE operation_fee_snapshots '
        'DISABLE TRIGGER trg_operation_fee_snapshot_immutable'
    )
    op.execute(
        """
        UPDATE operation_fee_snapshots snapshot
           SET merchant_id = deposit.merchant_id
          FROM deposits deposit
         WHERE deposit.id = snapshot.deposit_id
        """
    )
    op.execute(
        'ALTER TABLE operation_fee_snapshots '
        'ENABLE TRIGGER trg_operation_fee_snapshot_immutable'
    )
    incomplete_snapshots = bind.execute(sa.text(
        """
        SELECT count(*)
          FROM operation_fee_snapshots
         WHERE merchant_id IS NULL
            OR merchant_rate_rule_id IS NULL
            OR executor_rate_rule_id IS NULL
        """
    )).scalar_one()
    if incomplete_snapshots:
        raise RuntimeError('operation fee snapshots contain incomplete entity/rule references')

    op.alter_column(
        'operation_fee_snapshots', 'merchant_id',
        existing_type=postgresql.UUID(as_uuid=True), nullable=False,
    )
    op.create_foreign_key(
        'fk_operation_fee_snapshots_merchant_id',
        'operation_fee_snapshots', 'merchants', ['merchant_id'], ['id'], ondelete='RESTRICT',
    )
    op.create_index(
        'ix_operation_fee_snapshots_merchant_id',
        'operation_fee_snapshots', ['merchant_id'],
    )

    op.drop_constraint(
        'operation_fee_snapshots_merchant_rate_rule_id_fkey',
        'operation_fee_snapshots', type_='foreignkey',
    )
    op.drop_constraint(
        'operation_fee_snapshots_executor_rate_rule_id_fkey',
        'operation_fee_snapshots', type_='foreignkey',
    )
    op.alter_column(
        'operation_fee_snapshots', 'merchant_rate_rule_id',
        existing_type=postgresql.UUID(as_uuid=True), nullable=False,
    )
    op.alter_column(
        'operation_fee_snapshots', 'executor_rate_rule_id',
        existing_type=postgresql.UUID(as_uuid=True), nullable=False,
    )
    op.create_foreign_key(
        'fk_operation_fee_snapshots_merchant_rate_rule_id',
        'operation_fee_snapshots', 'fee_rules', ['merchant_rate_rule_id'], ['id'], ondelete='RESTRICT',
    )
    op.create_foreign_key(
        'fk_operation_fee_snapshots_executor_rate_rule_id',
        'operation_fee_snapshots', 'fee_rules', ['executor_rate_rule_id'], ['id'], ondelete='RESTRICT',
    )


def downgrade():
    op.drop_constraint(
        'fk_operation_fee_snapshots_executor_rate_rule_id',
        'operation_fee_snapshots', type_='foreignkey',
    )
    op.drop_constraint(
        'fk_operation_fee_snapshots_merchant_rate_rule_id',
        'operation_fee_snapshots', type_='foreignkey',
    )
    op.alter_column(
        'operation_fee_snapshots', 'executor_rate_rule_id',
        existing_type=postgresql.UUID(as_uuid=True), nullable=True,
    )
    op.alter_column(
        'operation_fee_snapshots', 'merchant_rate_rule_id',
        existing_type=postgresql.UUID(as_uuid=True), nullable=True,
    )
    op.create_foreign_key(
        'operation_fee_snapshots_merchant_rate_rule_id_fkey',
        'operation_fee_snapshots', 'fee_rules', ['merchant_rate_rule_id'], ['id'], ondelete='SET NULL',
    )
    op.create_foreign_key(
        'operation_fee_snapshots_executor_rate_rule_id_fkey',
        'operation_fee_snapshots', 'fee_rules', ['executor_rate_rule_id'], ['id'], ondelete='SET NULL',
    )

    op.drop_index('ix_operation_fee_snapshots_merchant_id', table_name='operation_fee_snapshots')
    op.drop_constraint(
        'fk_operation_fee_snapshots_merchant_id',
        'operation_fee_snapshots', type_='foreignkey',
    )
    op.drop_column('operation_fee_snapshots', 'merchant_id')

    op.drop_index('uq_fee_rules_active_exact', table_name='fee_rules')
    op.drop_index('ix_fee_rules_entity_scope', table_name='fee_rules')
    op.drop_constraint('ck_fee_rules_entity_fee_side', 'fee_rules', type_='check')
    op.drop_constraint('ck_fee_rules_entity_id_nonzero', 'fee_rules', type_='check')
    op.drop_constraint('ck_fee_rules_entity_type', 'fee_rules', type_='check')
    op.alter_column('fee_rules', 'entity_id', existing_type=postgresql.UUID(as_uuid=True), nullable=True)
