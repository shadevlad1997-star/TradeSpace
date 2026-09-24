"""Harden merchant settlement quotes and completion evidence.

Revision ID: 0021_settlement_hardening
Revises: 0020_rolling_confirmation_flow
"""

from alembic import op
import sqlalchemy as sa


revision = '0021_settlement_hardening'
down_revision = '0020_rolling_confirmation_flow'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'merchant_settlements',
        sa.Column('network', sa.String(length=32), nullable=False, server_default='TRC20'),
    )
    op.add_column(
        'merchant_settlements',
        sa.Column('tx_hash', sa.String(length=160), nullable=True),
    )
    op.add_column(
        'merchant_settlements',
        sa.Column('idempotency_key', sa.String(length=180), nullable=True),
    )
    op.add_column(
        'merchant_settlements',
        sa.Column('rate_symbol', sa.String(length=32), nullable=True),
    )
    op.add_column(
        'merchant_settlements',
        sa.Column('rate_side', sa.String(length=16), nullable=True),
    )
    op.add_column(
        'merchant_settlements',
        sa.Column('rate_source', sa.String(length=32), nullable=True),
    )
    op.add_column(
        'merchant_settlements',
        sa.Column('rate_provider_field', sa.String(length=32), nullable=True),
    )
    op.add_column(
        'merchant_settlements',
        sa.Column('provider_timestamp', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'merchant_settlements',
        sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'merchant_settlements',
        sa.Column('freshness_basis', sa.String(length=32), nullable=True),
    )
    op.execute(
        """
        UPDATE merchant_settlements
           SET idempotency_key = 'legacy:' || id::text
         WHERE idempotency_key IS NULL
        """
    )
    op.alter_column(
        'merchant_settlements',
        'idempotency_key',
        existing_type=sa.String(length=180),
        nullable=False,
    )
    op.create_unique_constraint(
        'uq_merchant_settlement_idempotency',
        'merchant_settlements',
        ['merchant_id', 'idempotency_key'],
    )
    op.create_index(
        'uq_merchant_settlement_completed_network_tx_hash',
        'merchant_settlements',
        ['network', 'tx_hash'],
        unique=True,
        postgresql_where=sa.text(
            "status = 'completed' AND tx_hash IS NOT NULL"
        ),
    )


def downgrade():
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                  FROM merchant_settlements
                 WHERE tx_hash IS NOT NULL
                    OR rate_source = 'rapira_live'
                    OR idempotency_key NOT LIKE 'legacy:%'
            ) THEN
                RAISE EXCEPTION
                    'merchant settlement hardening downgrade blocked: '
                    'post-upgrade settlement evidence exists';
            END IF;
        END
        $$;
        """
    )
    op.drop_index(
        'uq_merchant_settlement_completed_network_tx_hash',
        table_name='merchant_settlements',
    )
    op.drop_constraint(
        'uq_merchant_settlement_idempotency',
        'merchant_settlements',
        type_='unique',
    )
    for column in (
        'freshness_basis',
        'fetched_at',
        'provider_timestamp',
        'rate_provider_field',
        'rate_source',
        'rate_side',
        'rate_symbol',
        'idempotency_key',
        'tx_hash',
        'network',
    ):
        op.drop_column('merchant_settlements', column)
