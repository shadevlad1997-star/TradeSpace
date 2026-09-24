"""Persist strict Rolling Rapira freshness evidence.

Revision ID: 0016_rolling_rapira_freshness
Revises: 0015_merchant_rolling
"""

from alembic import op
import sqlalchemy as sa


revision = '0016_rolling_rapira_freshness'
down_revision = '0015_merchant_rolling'
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = 'merchant_rolling_allocations'
    op.add_column(
        table,
        sa.Column('rapira_rate_symbol', sa.String(length=16), nullable=True),
    )
    op.add_column(
        table,
        sa.Column(
            'rapira_provider_timestamp',
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        table,
        sa.Column(
            'rapira_fetched_at',
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        table,
        sa.Column(
            'rapira_freshness_basis',
            sa.String(length=32),
            nullable=True,
        ),
    )

    op.execute(
        """
        UPDATE merchant_rolling_allocations
           SET rapira_rate_symbol = 'USDT/RUB',
               rapira_provider_timestamp = rapira_rate_updated_at,
               rapira_fetched_at = created_at,
               rapira_freshness_basis = 'provider_timestamp',
               rapira_rate_source = CASE
                   WHEN rapira_rate_source = 'Rapira' THEN 'rapira_live'
                   ELSE rapira_rate_source
               END
        """
    )
    op.alter_column(table, 'rapira_rate_symbol', nullable=False)
    op.alter_column(table, 'rapira_fetched_at', nullable=False)
    op.alter_column(table, 'rapira_freshness_basis', nullable=False)
    op.create_check_constraint(
        'ck_rolling_allocation_freshness_basis',
        table,
        "rapira_freshness_basis IN ('provider_timestamp','fetched_at')",
    )


def downgrade() -> None:
    table = 'merchant_rolling_allocations'
    op.drop_constraint(
        'ck_rolling_allocation_freshness_basis',
        table,
        type_='check',
    )
    op.drop_column(table, 'rapira_freshness_basis')
    op.drop_column(table, 'rapira_fetched_at')
    op.drop_column(table, 'rapira_provider_timestamp')
    op.drop_column(table, 'rapira_rate_symbol')
