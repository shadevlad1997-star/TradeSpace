"""Harden webhook delivery leases and signing credentials.

Revision ID: 0023_webhook_hardening
Revises: 0022_hmac_v2_idempotency
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0023_webhook_hardening'
down_revision = '0022_hmac_v2_idempotency'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'merchant_webhook_signing_keys',
        sa.Column(
            'id',
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            'merchant_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('merchants.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('key_id', sa.String(length=80), nullable=False),
        sa.Column('encrypted_secret', sa.String(length=255), nullable=False),
        sa.Column(
            'status',
            sa.String(length=16),
            nullable=False,
            server_default='active',
        ),
        sa.Column(
            'retire_at',
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            'revoked_at',
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            'created_by',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('users.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active','retiring','revoked')",
            name='ck_merchant_webhook_signing_keys_status',
        ),
    )
    op.create_index(
        'ix_merchant_webhook_signing_keys_merchant_id',
        'merchant_webhook_signing_keys',
        ['merchant_id'],
    )
    op.create_index(
        'ix_merchant_webhook_signing_keys_key_id',
        'merchant_webhook_signing_keys',
        ['key_id'],
        unique=True,
    )
    op.create_index(
        'ix_merchant_webhook_signing_keys_status',
        'merchant_webhook_signing_keys',
        ['status'],
    )
    op.create_index(
        'ix_merchant_webhook_signing_keys_retire_at',
        'merchant_webhook_signing_keys',
        ['retire_at'],
    )
    op.create_index(
        'ix_merchant_webhook_signing_keys_created_by',
        'merchant_webhook_signing_keys',
        ['created_by'],
    )
    op.create_index(
        'uq_merchant_webhook_signing_keys_one_active',
        'merchant_webhook_signing_keys',
        ['merchant_id'],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.add_column(
        'webhook_events',
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'webhook_events',
        sa.Column(
            'max_attempts',
            sa.Integer(),
            nullable=False,
            server_default='5',
        ),
    )
    op.add_column(
        'webhook_events',
        sa.Column('lock_owner', sa.String(length=64), nullable=True),
    )
    op.add_column(
        'webhook_events',
        sa.Column('locked_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'webhook_events',
        sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'webhook_events',
        sa.Column('correlation_id', sa.String(length=64), nullable=True),
    )
    op.add_column(
        'webhook_events',
        sa.Column(
            'signing_key_id',
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        'fk_webhook_events_signing_key_id',
        'webhook_events',
        'merchant_webhook_signing_keys',
        ['signing_key_id'],
        ['id'],
        ondelete='SET NULL',
    )
    op.execute(
        """
        UPDATE webhook_events
           SET next_attempt_at = next_retry_at,
               max_attempts = GREATEST(attempts, 5),
               correlation_id = id::text,
               lease_until = CASE
                   WHEN status IN ('delivering', 'processing')
                       THEN COALESCE(next_retry_at, now())
                   ELSE NULL
               END,
               status = CASE
                   WHEN status IN ('queued', 'pending') THEN 'pending'
                   WHEN status IN ('delivering', 'processing') THEN 'processing'
                   WHEN status = 'failed' AND attempts >= 5
                       THEN 'dead_letter'
                   WHEN status IN ('failed', 'retry', 'retry_scheduled')
                       THEN 'retry_scheduled'
                   WHEN status = 'delivered' THEN 'delivered'
                   WHEN status IN ('dead_letter', 'blocked')
                       THEN 'dead_letter'
                   WHEN status IN (
                       'configuration_error',
                       'configuration_required',
                       'skipped'
                   ) THEN 'configuration_required'
                   ELSE 'dead_letter'
               END
        """
    )
    op.alter_column(
        'webhook_events',
        'correlation_id',
        nullable=False,
    )
    op.create_index(
        'ix_webhook_events_next_attempt_at',
        'webhook_events',
        ['next_attempt_at'],
    )
    op.create_index(
        'ix_webhook_events_lease_until',
        'webhook_events',
        ['lease_until'],
    )
    op.create_index(
        'ix_webhook_events_signing_key_id',
        'webhook_events',
        ['signing_key_id'],
    )
    op.create_index(
        'ix_webhook_events_delivery_due',
        'webhook_events',
        ['status', 'next_attempt_at', 'lease_until'],
    )
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY webhook_event_id
                       ORDER BY created_at, id
                   ) AS normalized_attempt_no
              FROM webhook_delivery_attempts
        )
        UPDATE webhook_delivery_attempts AS attempt
           SET attempt_no = ranked.normalized_attempt_no
          FROM ranked
         WHERE attempt.id = ranked.id
        """
    )
    op.execute(
        """
        UPDATE webhook_events AS event
           SET attempts = GREATEST(
                   event.attempts,
                   COALESCE(attempts.max_attempt_no, 0)
               ),
               max_attempts = GREATEST(
                   event.max_attempts,
                   event.attempts,
                   COALESCE(attempts.max_attempt_no, 0)
               )
          FROM (
              SELECT webhook_event_id, max(attempt_no) AS max_attempt_no
                FROM webhook_delivery_attempts
               GROUP BY webhook_event_id
          ) AS attempts
         WHERE event.id = attempts.webhook_event_id
        """
    )
    op.create_unique_constraint(
        'uq_webhook_delivery_attempt_event_number',
        'webhook_delivery_attempts',
        ['webhook_event_id', 'attempt_no'],
    )


def downgrade():
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM merchant_webhook_signing_keys
            ) THEN
                RAISE EXCEPTION
                    'webhook hardening downgrade blocked: signing keys exist';
            END IF;
            IF EXISTS (
                SELECT 1 FROM webhook_events
                 WHERE status = 'processing'
                    OR lock_owner IS NOT NULL
                    OR signing_key_id IS NOT NULL
            ) THEN
                RAISE EXCEPTION
                    'webhook hardening downgrade blocked: active leases or '
                    'signing key references exist';
            END IF;
        END
        $$;
        """
    )
    op.execute(
        """
        UPDATE webhook_events
           SET status = CASE
               WHEN status = 'pending' THEN 'queued'
               WHEN status = 'processing' THEN 'delivering'
               WHEN status = 'retry_scheduled' THEN 'failed'
               WHEN status = 'configuration_required'
                   THEN 'configuration_error'
               ELSE status
           END,
               next_retry_at = next_attempt_at
        """
    )
    op.drop_constraint(
        'uq_webhook_delivery_attempt_event_number',
        'webhook_delivery_attempts',
        type_='unique',
    )
    op.drop_index('ix_webhook_events_delivery_due', table_name='webhook_events')
    op.drop_index('ix_webhook_events_signing_key_id', table_name='webhook_events')
    op.drop_index('ix_webhook_events_lease_until', table_name='webhook_events')
    op.drop_index('ix_webhook_events_next_attempt_at', table_name='webhook_events')
    op.drop_constraint(
        'fk_webhook_events_signing_key_id',
        'webhook_events',
        type_='foreignkey',
    )
    for column in (
        'signing_key_id',
        'correlation_id',
        'lease_until',
        'locked_at',
        'lock_owner',
        'max_attempts',
        'next_attempt_at',
    ):
        op.drop_column('webhook_events', column)

    op.drop_index(
        'uq_merchant_webhook_signing_keys_one_active',
        table_name='merchant_webhook_signing_keys',
    )
    op.drop_index(
        'ix_merchant_webhook_signing_keys_created_by',
        table_name='merchant_webhook_signing_keys',
    )
    op.drop_index(
        'ix_merchant_webhook_signing_keys_retire_at',
        table_name='merchant_webhook_signing_keys',
    )
    op.drop_index(
        'ix_merchant_webhook_signing_keys_status',
        table_name='merchant_webhook_signing_keys',
    )
    op.drop_index(
        'ix_merchant_webhook_signing_keys_key_id',
        table_name='merchant_webhook_signing_keys',
    )
    op.drop_index(
        'ix_merchant_webhook_signing_keys_merchant_id',
        table_name='merchant_webhook_signing_keys',
    )
    op.drop_table('merchant_webhook_signing_keys')
