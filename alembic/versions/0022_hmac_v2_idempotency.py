"""Add immutable merchant API request fingerprints.

Revision ID: 0022_hmac_v2_idempotency
Revises: 0021_settlement_hardening
"""

from alembic import op
import sqlalchemy as sa


revision = '0022_hmac_v2_idempotency'
down_revision = '0021_settlement_hardening'
branch_labels = None
depends_on = None


def upgrade():
    for table in ('deposits', 'payouts'):
        op.add_column(
            table,
            sa.Column(
                'request_fingerprint',
                sa.String(length=64),
                nullable=True,
            ),
        )
        op.create_check_constraint(
            f'ck_{table}_request_fingerprint',
            table,
            (
                'request_fingerprint IS NULL '
                'OR length(request_fingerprint) = 64'
            ),
        )


def downgrade():
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM deposits
                 WHERE request_fingerprint IS NOT NULL
            ) OR EXISTS (
                SELECT 1 FROM payouts
                 WHERE request_fingerprint IS NOT NULL
            ) THEN
                RAISE EXCEPTION
                    'HMAC v2 idempotency downgrade blocked: '
                    'request fingerprints exist';
            END IF;
        END
        $$;
        """
    )
    for table in ('payouts', 'deposits'):
        op.drop_constraint(
            f'ck_{table}_request_fingerprint',
            table,
            type_='check',
        )
        op.drop_column(table, 'request_fingerprint')
