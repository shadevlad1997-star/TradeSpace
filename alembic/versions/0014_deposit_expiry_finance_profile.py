"""Add immutable deposit deadlines and merchant finance profiles.

Revision ID: 0014_deposit_finance
Revises: 0013_encrypt_secrets
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0014_deposit_finance'
down_revision = '0013_encrypt_secrets'
branch_labels = None
depends_on = None


_EXPIRES_BACKFILL_BATCH_SIZE = 10_000


def _backfill_expires_at() -> None:
    """Backfill in committed batches so the migration is restartable."""
    context = op.get_context()
    batch_sql = sa.text(
        f"""
        WITH batch AS (
            SELECT id
              FROM deposits
             WHERE expires_at IS NULL
             ORDER BY id
             LIMIT {_EXPIRES_BACKFILL_BATCH_SIZE}
        )
        UPDATE deposits AS target
           SET expires_at = target.created_at + interval '15 minutes'
          FROM batch
         WHERE target.id = batch.id
        """
    )
    if context.as_sql:
        # Offline SQL cannot execute a Python rowcount loop. A top-level
        # PostgreSQL procedure provides the same per-batch commits.
        op.execute(
            f"""
            CREATE PROCEDURE backfill_deposit_expires_at_0014()
            LANGUAGE plpgsql
            AS $$
            DECLARE
                changed_rows bigint;
            BEGIN
                LOOP
                    WITH batch AS (
                        SELECT id
                          FROM deposits
                         WHERE expires_at IS NULL
                         ORDER BY id
                         LIMIT {_EXPIRES_BACKFILL_BATCH_SIZE}
                    )
                    UPDATE deposits AS target
                       SET expires_at = target.created_at + interval '15 minutes'
                      FROM batch
                     WHERE target.id = batch.id;
                    GET DIAGNOSTICS changed_rows = ROW_COUNT;
                    COMMIT;
                    EXIT WHEN changed_rows = 0;
                END LOOP;
            END;
            $$;
            CALL backfill_deposit_expires_at_0014();
            DROP PROCEDURE backfill_deposit_expires_at_0014();
            """
        )
        return

    bind = op.get_bind()
    while True:
        result = bind.execute(batch_sql)
        if result.rowcount == 0:
            break


def upgrade() -> None:
    # Raw IF NOT EXISTS makes a partially completed non-transactional backfill
    # safe to resume. Add the volatile default separately to avoid a table
    # rewrite while the column is introduced.
    op.execute(
        """
        ALTER TABLE deposits
        ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ
        """
    )
    op.execute(
        """
        ALTER TABLE deposits
        ALTER COLUMN expires_at
        SET DEFAULT now() + interval '15 minutes'
        """
    )
    with op.get_context().autocommit_block():
        _backfill_expires_at()

    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                  FROM pg_constraint
                 WHERE conrelid = 'deposits'::regclass
                   AND conname = 'ck_deposits_expires_at_not_null'
            ) THEN
                ALTER TABLE deposits
                ADD CONSTRAINT ck_deposits_expires_at_not_null
                CHECK (expires_at IS NOT NULL) NOT VALID;
            END IF;
        END
        $$;
        """
    )
    op.execute(
        """
        ALTER TABLE deposits
        VALIDATE CONSTRAINT ck_deposits_expires_at_not_null
        """
    )
    op.alter_column(
        'deposits',
        'expires_at',
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )
    op.execute(
        """
        ALTER TABLE deposits
        DROP CONSTRAINT ck_deposits_expires_at_not_null
        """
    )
    with op.get_context().autocommit_block():
        op.execute('DROP INDEX CONCURRENTLY IF EXISTS ix_deposits_expires_at')
        op.execute(
            'CREATE INDEX CONCURRENTLY ix_deposits_expires_at '
            'ON deposits (expires_at)'
        )
        op.execute(
            'DROP INDEX CONCURRENTLY IF EXISTS ix_deposits_status_expires_at'
        )
        op.execute(
            'CREATE INDEX CONCURRENTLY ix_deposits_status_expires_at '
            'ON deposits (status, expires_at)'
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
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
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
        SELECT gen_random_uuid(), id, 'postpaid', true, now(), now()
          FROM merchants
        ON CONFLICT (merchant_id) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_index(
        'ix_merchant_finance_profiles_settlement_mode',
        table_name='merchant_finance_profiles',
    )
    op.drop_table('merchant_finance_profiles')
    with op.get_context().autocommit_block():
        op.execute(
            'DROP INDEX CONCURRENTLY IF EXISTS ix_deposits_status_expires_at'
        )
        op.execute('DROP INDEX CONCURRENTLY IF EXISTS ix_deposits_expires_at')
    op.drop_column('deposits', 'expires_at')
