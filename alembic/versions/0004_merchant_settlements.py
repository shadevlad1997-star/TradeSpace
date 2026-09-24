"""merchant commission and settlements

Revision ID: 0004_merchant_settlements
Revises: 0003_trader_controls
Create Date: 2026-05-10
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0004_merchant_settlements'
down_revision = '0003_trader_controls'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('merchants', sa.Column('merchant_commission_percent', sa.Numeric(5, 2), nullable=False, server_default='15.00'))
    op.create_table(
        'merchant_settlements',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('merchant_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('merchants.id', ondelete='CASCADE'), nullable=False),
        sa.Column('requested_by_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('processed_by_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('amount_usdt', sa.Numeric(18, 2), nullable=False),
        sa.Column('fee_usdt', sa.Numeric(18, 2), nullable=False, server_default='5.00'),
        sa.Column('rate_rub', sa.Numeric(18, 4), nullable=False, server_default='100.0000'),
        sa.Column('amount_rub', sa.Numeric(18, 2), nullable=False),
        sa.Column('fee_rub', sa.Numeric(18, 2), nullable=False),
        sa.Column('total_debit_rub', sa.Numeric(18, 2), nullable=False),
        sa.Column('trc20_address', sa.String(128), nullable=False),
        sa.Column('status', sa.String(32), nullable=False, server_default='pending'),
        sa.Column('reject_reason', sa.String(500), nullable=True),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('metadata_json', postgresql.JSON(), nullable=False, server_default='{}'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_merchant_settlements_merchant_id', 'merchant_settlements', ['merchant_id'])
    op.create_index('ix_merchant_settlements_requested_by_id', 'merchant_settlements', ['requested_by_id'])
    op.create_index('ix_merchant_settlements_processed_by_id', 'merchant_settlements', ['processed_by_id'])
    op.create_index('ix_merchant_settlements_status', 'merchant_settlements', ['status'])


def downgrade():
    op.drop_index('ix_merchant_settlements_status', table_name='merchant_settlements')
    op.drop_index('ix_merchant_settlements_processed_by_id', table_name='merchant_settlements')
    op.drop_index('ix_merchant_settlements_requested_by_id', table_name='merchant_settlements')
    op.drop_index('ix_merchant_settlements_merchant_id', table_name='merchant_settlements')
    op.drop_table('merchant_settlements')
    op.drop_column('merchants', 'merchant_commission_percent')