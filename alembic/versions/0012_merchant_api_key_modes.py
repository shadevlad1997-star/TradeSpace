"""Add production-ready merchant API key lifecycle fields.

Revision ID: 0012_merchant_api_key_modes
Revises: 0011_individual_fee_rules
"""

from alembic import op
import sqlalchemy as sa


revision = '0012_merchant_api_key_modes'
down_revision = '0011_individual_fee_rules'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('api_keys', sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('api_keys', sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True))
    op.execute(sa.text("UPDATE api_keys SET mode = 'production' WHERE lower(mode) IN ('live', 'prod')"))
    op.execute(sa.text("UPDATE api_keys SET mode = 'sandbox' WHERE lower(mode) IN ('test', 'dev', 'development')"))
    op.execute(sa.text(
        """
        UPDATE api_keys
           SET revoked_at = COALESCE(revoked_at, updated_at, created_at)
         WHERE is_active = false
        """
    ))
    op.execute(sa.text(
        """
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY merchant_id, mode
                       ORDER BY created_at DESC, id DESC
                   ) AS position
              FROM api_keys
             WHERE is_active = true
        )
        UPDATE api_keys target
           SET is_active = false,
               revoked_at = COALESCE(target.revoked_at, now())
          FROM ranked
         WHERE target.id = ranked.id
           AND ranked.position > 1
        """
    ))
    op.create_check_constraint(
        'ck_api_keys_mode',
        'api_keys',
        "mode IN ('sandbox', 'production')",
    )
    op.create_index(
        'uq_api_keys_one_active_per_merchant_mode',
        'api_keys',
        ['merchant_id', 'mode'],
        unique=True,
        postgresql_where=sa.text('is_active'),
    )


def downgrade():
    op.drop_index('uq_api_keys_one_active_per_merchant_mode', table_name='api_keys')
    op.drop_constraint('ck_api_keys_mode', 'api_keys', type_='check')
    op.drop_column('api_keys', 'revoked_at')
    op.drop_column('api_keys', 'last_used_at')
