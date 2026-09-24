"""Replace persistent settlement modes with confirmed Rolling transfers.

Revision ID: 0020_rolling_confirmation_flow
Revises: 0019_ai_office_config
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0020_rolling_confirmation_flow'
down_revision = '0019_ai_office_config'
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
    # Fail closed. The migration normalizes existing financial state but never
    # repairs contradictory balances or rewrites prior ledger entries.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                  FROM merchant_rolling_accounts
                 WHERE principal_usdt <> recovered_usdt + outstanding_usdt
            ) THEN
                RAISE EXCEPTION
                    'rolling migration invariant failed: account aggregate mismatch';
            END IF;

            IF EXISTS (
                SELECT 1
                  FROM merchant_rolling_accounts a
                 WHERE a.recovered_usdt <> COALESCE(
                    (
                        SELECT SUM(x.rolling_applied_usdt)
                          FROM merchant_rolling_allocations x
                         WHERE x.rolling_account_id = a.id
                           AND x.status = 'paid'
                    ),
                    0
                 )
            ) THEN
                RAISE EXCEPTION
                    'rolling migration invariant failed: paid allocation mismatch';
            END IF;

            IF EXISTS (
                SELECT 1
                  FROM merchant_rolling_accounts a
                 WHERE a.principal_usdt <> COALESCE(
                    (
                        SELECT SUM(l.amount_usdt)
                          FROM merchant_rolling_ledger_entries l
                         WHERE l.rolling_account_id = a.id
                           AND l.entry_type IN ('funding', 'topup')
                    ),
                    0
                 )
            ) THEN
                RAISE EXCEPTION
                    'rolling migration invariant failed: legacy funding aggregate mismatch';
            END IF;

            IF EXISTS (
                SELECT 1
                  FROM merchant_rolling_allocations x
                 WHERE x.status IN ('paid', 'reversed')
                   AND x.rolling_applied_rub + x.settle_credited_rub
                       <> x.merchant_net_rub
            ) THEN
                RAISE EXCEPTION
                    'rolling migration invariant failed: allocation RUB split mismatch';
            END IF;

            IF EXISTS (
                SELECT 1
                  FROM merchant_rolling_allocations x
                  JOIN merchant_rolling_accounts a
                    ON a.id = x.rolling_account_id
                 WHERE (
                        x.rolling_applied_usdt > 0
                        OR EXISTS (
                            SELECT 1
                              FROM merchant_rolling_ledger_entries l
                             WHERE l.deposit_id = x.deposit_id
                               AND l.entry_type = 'recovery'
                               AND COALESCE(l.amount_usdt, 0) > 0
                        )
                    )
                   AND a.principal_usdt <= 0
            ) THEN
                RAISE EXCEPTION
                    'rolling migration invariant failed: proven consumption lacks legacy funding';
            END IF;

            IF EXISTS (
                SELECT 1
                  FROM merchant_rolling_allocations x
                 WHERE x.rolling_applied_usdt = 0
                   AND EXISTS (
                        SELECT 1
                          FROM merchant_rolling_ledger_entries l
                         WHERE l.deposit_id = x.deposit_id
                           AND l.entry_type = 'recovery'
                           AND COALESCE(l.amount_usdt, 0) > 0
                    )
            ) THEN
                RAISE EXCEPTION
                    'rolling migration invariant failed: recovery evidence mismatch';
            END IF;
        END
        $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT merchant_id
                  FROM merchant_settlements
                 WHERE status = 'pending'
                 GROUP BY merchant_id
                HAVING COUNT(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'rolling migration invariant failed: duplicate pending settle';
            END IF;
        END
        $$;
        """
    )
    op.create_index(
        'uq_merchant_settlement_one_pending',
        'merchant_settlements',
        ['merchant_id'],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        'merchant_rolling_transfers',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
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
            nullable=True,
        ),
        sa.Column('sequence_no', sa.Integer(), nullable=False),
        sa.Column('amount_usdt', sa.Numeric(24, 6), nullable=False),
        sa.Column(
            'recovered_usdt',
            sa.Numeric(24, 6),
            nullable=False,
            server_default='0',
        ),
        sa.Column(
            'remaining_usdt',
            sa.Numeric(24, 6),
            nullable=False,
            server_default='0',
        ),
        sa.Column('network', sa.String(length=32), nullable=True),
        sa.Column('destination_address', sa.String(length=160), nullable=True),
        sa.Column('tx_hash', sa.String(length=160), nullable=True),
        sa.Column(
            'status',
            sa.String(length=32),
            nullable=False,
            server_default='pending_confirmation',
        ),
        sa.Column(
            'source',
            sa.String(length=32),
            nullable=False,
            server_default='registered',
        ),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('disputed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_by',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('users.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column(
            'confirmed_by',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('users.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column(
            'disputed_by',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('users.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column(
            'cancelled_by',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('users.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column('comment', sa.String(length=500), nullable=True),
        sa.Column('dispute_reason', sa.String(length=500), nullable=True),
        sa.Column('cancel_reason', sa.String(length=500), nullable=True),
        sa.Column('idempotency_key', sa.String(length=180), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            'merchant_id',
            'sequence_no',
            name='uq_rolling_transfer_merchant_sequence',
        ),
        sa.UniqueConstraint(
            'network',
            'tx_hash',
            name='uq_rolling_transfer_network_tx_hash',
        ),
        sa.UniqueConstraint(
            'idempotency_key',
            name='uq_rolling_transfer_idempotency_key',
        ),
        sa.CheckConstraint(
            'amount_usdt > 0',
            name='ck_rolling_transfer_amount_positive',
        ),
        sa.CheckConstraint(
            'recovered_usdt >= 0',
            name='ck_rolling_transfer_recovered_nonnegative',
        ),
        sa.CheckConstraint(
            'remaining_usdt >= 0',
            name='ck_rolling_transfer_remaining_nonnegative',
        ),
        sa.CheckConstraint(
            "(status = 'confirmed' "
            "AND rolling_account_id IS NOT NULL "
            "AND confirmed_at IS NOT NULL "
            "AND recovered_usdt + remaining_usdt = amount_usdt) "
            "OR (status IN ('pending_confirmation','disputed','cancelled') "
            "AND rolling_account_id IS NULL "
            "AND confirmed_at IS NULL "
            "AND recovered_usdt = 0 "
            "AND remaining_usdt = 0)",
            name='ck_rolling_transfer_financial_state',
        ),
        sa.CheckConstraint(
            "(source = 'legacy_migration' AND tx_hash IS NULL) "
            "OR (source = 'registered' AND network IS NOT NULL "
            "AND destination_address IS NOT NULL AND tx_hash IS NOT NULL)",
            name='ck_rolling_transfer_evidence',
        ),
        sa.CheckConstraint(
            "status IN ('pending_confirmation','confirmed','disputed','cancelled')",
            name='ck_rolling_transfer_status',
        ),
        sa.CheckConstraint(
            "source IN ('registered','legacy_migration')",
            name='ck_rolling_transfer_source',
        ),
    )
    op.create_index(
        'ix_merchant_rolling_transfers_merchant_id',
        'merchant_rolling_transfers',
        ['merchant_id'],
    )
    op.create_index(
        'ix_merchant_rolling_transfers_rolling_account_id',
        'merchant_rolling_transfers',
        ['rolling_account_id'],
    )
    op.create_index(
        'ix_merchant_rolling_transfers_status',
        'merchant_rolling_transfers',
        ['status'],
    )
    op.create_index(
        'ix_merchant_rolling_transfers_confirmed_at',
        'merchant_rolling_transfers',
        ['confirmed_at'],
    )
    op.create_index(
        'ix_rolling_transfer_merchant_status_sequence',
        'merchant_rolling_transfers',
        ['merchant_id', 'status', 'sequence_no'],
    )

    allocation_table = 'merchant_rolling_allocations'
    op.add_column(
        allocation_table,
        sa.Column(
            'eligibility_status',
            sa.String(length=16),
            nullable=True,
        ),
    )
    op.add_column(
        allocation_table,
        sa.Column('eligible_transfer_sequence', sa.Integer(), nullable=True),
    )
    op.add_column(
        allocation_table,
        sa.Column(
            'rolling_eligible_at',
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        allocation_table,
        sa.Column(
            'eligibility_source',
            sa.String(length=32),
            nullable=True,
        ),
    )

    op.add_column(
        'merchant_rolling_ledger_entries',
        sa.Column(
            'rolling_transfer_id',
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        'fk_rolling_ledger_transfer',
        'merchant_rolling_ledger_entries',
        'merchant_rolling_transfers',
        ['rolling_transfer_id'],
        ['id'],
        ondelete='RESTRICT',
    )
    op.create_index(
        'ix_merchant_rolling_ledger_entries_rolling_transfer_id',
        'merchant_rolling_ledger_entries',
        ['rolling_transfer_id'],
    )

    op.execute(
        """
        INSERT INTO merchant_rolling_transfers (
            id,
            merchant_id,
            rolling_account_id,
            sequence_no,
            amount_usdt,
            recovered_usdt,
            remaining_usdt,
            network,
            destination_address,
            tx_hash,
            status,
            source,
            sent_at,
            confirmed_at,
            idempotency_key,
            created_at,
            updated_at
        )
        SELECT
            gen_random_uuid(),
            a.merchant_id,
            a.id,
            1,
            a.principal_usdt,
            a.recovered_usdt,
            a.outstanding_usdt,
            NULL,
            NULL,
            NULL,
            'confirmed',
            'legacy_migration',
            COALESCE(
                (
                    SELECT MIN(evidence.evidence_at)
                      FROM (
                            SELECT d.created_at AS evidence_at
                              FROM merchant_rolling_allocations x
                              JOIN deposits d ON d.id = x.deposit_id
                             WHERE x.rolling_account_id = a.id
                               AND (
                                    x.rolling_applied_usdt > 0
                                    OR EXISTS (
                                        SELECT 1
                                          FROM merchant_rolling_ledger_entries r
                                         WHERE r.deposit_id = x.deposit_id
                                           AND r.entry_type = 'recovery'
                                           AND COALESCE(r.amount_usdt, 0) > 0
                                    )
                               )
                            UNION ALL
                            SELECT COALESCE(l.funded_at, l.created_at)
                              FROM merchant_rolling_ledger_entries l
                             WHERE l.rolling_account_id = a.id
                               AND l.entry_type IN ('funding', 'topup')
                      ) evidence
                ),
                a.created_at
            ),
            COALESCE(
                (
                    SELECT MIN(evidence.evidence_at)
                      FROM (
                            SELECT d.created_at AS evidence_at
                              FROM merchant_rolling_allocations x
                              JOIN deposits d ON d.id = x.deposit_id
                             WHERE x.rolling_account_id = a.id
                               AND (
                                    x.rolling_applied_usdt > 0
                                    OR EXISTS (
                                        SELECT 1
                                          FROM merchant_rolling_ledger_entries r
                                         WHERE r.deposit_id = x.deposit_id
                                           AND r.entry_type = 'recovery'
                                           AND COALESCE(r.amount_usdt, 0) > 0
                                    )
                               )
                            UNION ALL
                            SELECT COALESCE(l.funded_at, l.created_at)
                              FROM merchant_rolling_ledger_entries l
                             WHERE l.rolling_account_id = a.id
                               AND l.entry_type IN ('funding', 'topup')
                      ) evidence
                ),
                a.created_at
            ),
            'legacy-rolling-transfer:' || a.id::text,
            a.created_at,
            a.updated_at
          FROM merchant_rolling_accounts a
         WHERE a.principal_usdt > 0
        """
    )

    op.execute(
        """
        UPDATE merchant_rolling_allocations x
           SET eligibility_status = 'eligible',
               eligible_transfer_sequence = t.sequence_no,
               rolling_eligible_at = d.created_at,
               eligibility_source = 'legacy_migration'
          FROM merchant_rolling_transfers t,
               deposits d
         WHERE t.rolling_account_id = x.rolling_account_id
           AND t.source = 'legacy_migration'
           AND d.id = x.deposit_id
           AND (
                x.rolling_applied_usdt > 0
                OR EXISTS (
                    SELECT 1
                      FROM merchant_rolling_ledger_entries r
                     WHERE r.deposit_id = x.deposit_id
                       AND r.entry_type = 'recovery'
                       AND COALESCE(r.amount_usdt, 0) > 0
                )
           )
        """
    )
    op.execute(
        """
        UPDATE merchant_rolling_allocations x
           SET eligibility_status = 'eligible',
               eligible_transfer_sequence = t.sequence_no,
               rolling_eligible_at = d.created_at,
               eligibility_source = 'legacy_migration'
          FROM merchant_rolling_transfers t,
               deposits d
         WHERE x.eligibility_status IS NULL
           AND t.rolling_account_id = x.rolling_account_id
           AND t.source = 'legacy_migration'
           AND d.id = x.deposit_id
           AND EXISTS (
                SELECT 1
                  FROM merchant_rolling_ledger_entries l
                 WHERE l.rolling_account_id = x.rolling_account_id
                   AND l.entry_type IN ('funding', 'topup')
                   AND COALESCE(l.funded_at, l.created_at) <= d.created_at
           )
        """
    )
    op.execute(
        """
        UPDATE merchant_rolling_allocations
           SET eligibility_status = 'ineligible',
               eligible_transfer_sequence = NULL,
               rolling_eligible_at = NULL,
               eligibility_source = 'legacy_no_confirmed_funding'
         WHERE eligibility_status IS NULL
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                  FROM merchant_rolling_allocations
                 WHERE eligibility_status IS NULL
                    OR eligibility_source IS NULL
                    OR (
                        eligibility_status = 'eligible'
                        AND (
                            eligible_transfer_sequence IS NULL
                            OR rolling_eligible_at IS NULL
                        )
                    )
                    OR (
                        eligibility_status = 'ineligible'
                        AND (
                            eligible_transfer_sequence IS NOT NULL
                            OR rolling_eligible_at IS NOT NULL
                        )
                    )
            ) THEN
                RAISE EXCEPTION
                    'rolling migration invariant failed: invalid eligibility snapshot';
            END IF;

            IF EXISTS (
                SELECT 1
                  FROM merchant_rolling_allocations x
                 WHERE (
                        x.rolling_applied_usdt > 0
                        OR EXISTS (
                            SELECT 1
                              FROM merchant_rolling_ledger_entries l
                             WHERE l.deposit_id = x.deposit_id
                               AND l.entry_type = 'recovery'
                               AND COALESCE(l.amount_usdt, 0) > 0
                        )
                    )
                   AND x.eligibility_status <> 'eligible'
            ) THEN
                RAISE EXCEPTION
                    'rolling migration invariant failed: proven consumption has no transfer mapping';
            END IF;
        END
        $$;
        """
    )
    op.alter_column(
        allocation_table,
        'eligibility_status',
        nullable=False,
    )
    op.alter_column(allocation_table, 'eligibility_source', nullable=False)
    op.create_check_constraint(
        'ck_rolling_allocation_eligibility_state',
        allocation_table,
        "(eligibility_status = 'eligible' "
        "AND eligible_transfer_sequence > 0 "
        "AND rolling_eligible_at IS NOT NULL "
        "AND eligibility_source IN "
        "('confirmed_transfer','legacy_migration')) "
        "OR (eligibility_status = 'ineligible' "
        "AND eligible_transfer_sequence IS NULL "
        "AND rolling_eligible_at IS NULL "
        "AND eligibility_source IN "
        "('legacy_no_confirmed_funding',"
        "'no_confirmed_funding_at_creation'))",
    )

    op.create_table(
        'merchant_rolling_transfer_consumptions',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
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
        sa.Column(
            'rolling_transfer_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('merchant_rolling_transfers.id', ondelete='RESTRICT'),
            nullable=False,
        ),
        sa.Column(
            'rolling_allocation_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('merchant_rolling_allocations.id', ondelete='RESTRICT'),
            nullable=False,
        ),
        sa.Column(
            'deposit_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('deposits.id', ondelete='RESTRICT'),
            nullable=False,
        ),
        sa.Column('entry_type', sa.String(length=16), nullable=False),
        sa.Column('amount_usdt', sa.Numeric(24, 6), nullable=False),
        sa.Column('amount_rub', sa.Numeric(18, 2), nullable=False),
        sa.Column('rate_rub', sa.Numeric(24, 8), nullable=False),
        sa.Column('idempotency_key', sa.String(length=180), nullable=False),
        sa.Column('reason', sa.String(length=500), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            'idempotency_key',
            name='uq_rolling_consumption_idempotency_key',
        ),
        sa.UniqueConstraint(
            'rolling_transfer_id',
            'deposit_id',
            'entry_type',
            name='uq_rolling_consumption_transfer_deposit_type',
        ),
        sa.CheckConstraint(
            "entry_type IN ('recovery','reversal')",
            name='ck_rolling_consumption_entry_type',
        ),
        sa.CheckConstraint(
            'amount_usdt > 0',
            name='ck_rolling_consumption_amount_usdt_positive',
        ),
        sa.CheckConstraint(
            'amount_rub >= 0',
            name='ck_rolling_consumption_amount_rub_nonnegative',
        ),
        sa.CheckConstraint(
            'rate_rub > 0',
            name='ck_rolling_consumption_rate_positive',
        ),
    )
    for name, columns in (
        (
            'ix_merchant_rolling_transfer_consumptions_merchant_id',
            ['merchant_id'],
        ),
        (
            'ix_merchant_rolling_transfer_consumptions_rolling_account_id',
            ['rolling_account_id'],
        ),
        (
            'ix_merchant_rolling_transfer_consumptions_rolling_transfer_id',
            ['rolling_transfer_id'],
        ),
        (
            'ix_merchant_rolling_transfer_consumptions_rolling_allocation_id',
            ['rolling_allocation_id'],
        ),
        (
            'ix_merchant_rolling_transfer_consumptions_deposit_id',
            ['deposit_id'],
        ),
        (
            'ix_rolling_consumption_transfer_created',
            ['rolling_transfer_id', 'created_at'],
        ),
    ):
        op.create_index(
            name,
            'merchant_rolling_transfer_consumptions',
            columns,
        )

    # Derived immutable evidence for historical paid/reversed allocations.
    op.execute(
        """
        INSERT INTO merchant_rolling_transfer_consumptions (
            id,
            merchant_id,
            rolling_account_id,
            rolling_transfer_id,
            rolling_allocation_id,
            deposit_id,
            entry_type,
            amount_usdt,
            amount_rub,
            rate_rub,
            idempotency_key,
            reason,
            created_at,
            updated_at
        )
        SELECT
            gen_random_uuid(),
            x.merchant_id,
            x.rolling_account_id,
            t.id,
            x.id,
            x.deposit_id,
            'recovery',
            x.rolling_applied_usdt,
            x.rolling_applied_rub,
            x.rapira_rate_rub,
            'legacy-rolling-consumption-' || x.id::text || '-recovery',
            'legacy allocation recovery evidence',
            COALESCE(x.finalized_at, x.created_at),
            COALESCE(x.finalized_at, x.updated_at)
          FROM merchant_rolling_allocations x
          JOIN merchant_rolling_transfers t
            ON t.rolling_account_id = x.rolling_account_id
           AND t.source = 'legacy_migration'
         WHERE x.status IN ('paid','reversed')
           AND x.rolling_applied_usdt > 0
        """
    )
    op.execute(
        """
        INSERT INTO merchant_rolling_transfer_consumptions (
            id,
            merchant_id,
            rolling_account_id,
            rolling_transfer_id,
            rolling_allocation_id,
            deposit_id,
            entry_type,
            amount_usdt,
            amount_rub,
            rate_rub,
            idempotency_key,
            reason,
            created_at,
            updated_at
        )
        SELECT
            gen_random_uuid(),
            x.merchant_id,
            x.rolling_account_id,
            t.id,
            x.id,
            x.deposit_id,
            'reversal',
            x.rolling_applied_usdt,
            x.rolling_applied_rub,
            x.rapira_rate_rub,
            'legacy-rolling-consumption-' || x.id::text || '-reversal',
            'legacy allocation reversal evidence',
            COALESCE(x.finalized_at, x.updated_at),
            COALESCE(x.finalized_at, x.updated_at)
          FROM merchant_rolling_allocations x
          JOIN merchant_rolling_transfers t
            ON t.rolling_account_id = x.rolling_account_id
           AND t.source = 'legacy_migration'
         WHERE x.status = 'reversed'
           AND x.rolling_applied_usdt > 0
        """
    )
    op.execute(
        """
        CREATE FUNCTION protect_rolling_consumption_immutable()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'rolling transfer consumption rows are immutable';
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_rolling_consumption_immutable
        BEFORE UPDATE OR DELETE ON merchant_rolling_transfer_consumptions
        FOR EACH ROW EXECUTE FUNCTION protect_rolling_consumption_immutable();
        """
    )

    op.drop_constraint(
        'ck_rolling_account_status',
        'merchant_rolling_accounts',
        type_='check',
    )
    op.execute(
        """
        UPDATE merchant_rolling_accounts
           SET status = CASE
               WHEN status = 'suspended' THEN 'suspended'
               WHEN outstanding_usdt > 0 THEN 'active'
               ELSE 'exhausted'
           END
        """
    )
    op.create_check_constraint(
        'ck_rolling_account_status',
        'merchant_rolling_accounts',
        "status IN ('active','exhausted','suspended')",
    )

    # This entity had no settings beyond the removed persistent mode.
    op.drop_index(
        'ix_merchant_finance_profiles_settlement_mode',
        table_name='merchant_finance_profiles',
    )
    op.drop_table('merchant_finance_profiles')


def downgrade() -> None:
    # New registered transfers are financial/audit evidence. A destructive
    # downgrade is refused; legacy-only normalization can be removed safely.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                  FROM merchant_rolling_transfers
                 WHERE source = 'registered'
            ) THEN
                RAISE EXCEPTION
                    'refusing destructive Rolling downgrade after transfer registration';
            END IF;
        END
        $$;
        """
    )
    op.drop_index(
        'uq_merchant_settlement_one_pending',
        table_name='merchant_settlements',
    )

    op.create_table(
        'merchant_finance_profiles',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            'merchant_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('merchants.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column(
            'settlement_mode',
            sa.String(length=16),
            nullable=False,
            server_default='postpaid',
        ),
        sa.Column(
            'is_active',
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            'created_by',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('users.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column(
            'updated_by',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('users.id', ondelete='SET NULL'),
            nullable=True,
        ),
        *_timestamps(),
        sa.UniqueConstraint(
            'merchant_id',
            name='uq_merchant_finance_profiles_merchant_id',
        ),
        sa.CheckConstraint(
            "settlement_mode IN ('postpaid','rolling')",
            name='ck_merchant_finance_profiles_mode',
        ),
    )
    op.create_index(
        'ix_merchant_finance_profiles_settlement_mode',
        'merchant_finance_profiles',
        ['settlement_mode'],
    )
    op.execute(
        """
        INSERT INTO merchant_finance_profiles (
            id,
            merchant_id,
            settlement_mode,
            is_active,
            created_at,
            updated_at
        )
        SELECT
            gen_random_uuid(),
            m.id,
            CASE WHEN a.principal_usdt > 0 THEN 'rolling' ELSE 'postpaid' END,
            true,
            now(),
            now()
          FROM merchants m
          LEFT JOIN merchant_rolling_accounts a ON a.merchant_id = m.id
        """
    )

    op.drop_constraint(
        'ck_rolling_account_status',
        'merchant_rolling_accounts',
        type_='check',
    )
    op.create_check_constraint(
        'ck_rolling_account_status',
        'merchant_rolling_accounts',
        "status IN ('active','exhausted','suspended','closed')",
    )

    op.execute(
        'DROP TRIGGER IF EXISTS trg_rolling_consumption_immutable '
        'ON merchant_rolling_transfer_consumptions'
    )
    op.execute(
        'DROP FUNCTION IF EXISTS protect_rolling_consumption_immutable()'
    )
    for name in (
        'ix_rolling_consumption_transfer_created',
        'ix_merchant_rolling_transfer_consumptions_deposit_id',
        'ix_merchant_rolling_transfer_consumptions_rolling_allocation_id',
        'ix_merchant_rolling_transfer_consumptions_rolling_transfer_id',
        'ix_merchant_rolling_transfer_consumptions_rolling_account_id',
        'ix_merchant_rolling_transfer_consumptions_merchant_id',
    ):
        op.drop_index(
            name,
            table_name='merchant_rolling_transfer_consumptions',
        )
    op.drop_table('merchant_rolling_transfer_consumptions')

    op.drop_constraint(
        'ck_rolling_allocation_eligibility_state',
        'merchant_rolling_allocations',
        type_='check',
    )
    op.drop_column('merchant_rolling_allocations', 'eligibility_source')
    op.drop_column('merchant_rolling_allocations', 'rolling_eligible_at')
    op.drop_column(
        'merchant_rolling_allocations',
        'eligible_transfer_sequence',
    )
    op.drop_column('merchant_rolling_allocations', 'eligibility_status')

    op.drop_index(
        'ix_merchant_rolling_ledger_entries_rolling_transfer_id',
        table_name='merchant_rolling_ledger_entries',
    )
    op.drop_constraint(
        'fk_rolling_ledger_transfer',
        'merchant_rolling_ledger_entries',
        type_='foreignkey',
    )
    op.drop_column(
        'merchant_rolling_ledger_entries',
        'rolling_transfer_id',
    )

    for name in (
        'ix_rolling_transfer_merchant_status_sequence',
        'ix_merchant_rolling_transfers_confirmed_at',
        'ix_merchant_rolling_transfers_status',
        'ix_merchant_rolling_transfers_rolling_account_id',
        'ix_merchant_rolling_transfers_merchant_id',
    ):
        op.drop_index(name, table_name='merchant_rolling_transfers')
    op.drop_table('merchant_rolling_transfers')
