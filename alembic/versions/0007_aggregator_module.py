"""aggregator module

Revision ID: 0007_aggregator_module
Revises: 0006_money_safety_hardening
Create Date: 2026-06-23
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0007_aggregator_module'
down_revision = '0006_money_safety_hardening'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'aggregator_accounts',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('platform_merchant_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('merchants.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('name', sa.String(length=160), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='active'),
        sa.Column('api_key', sa.String(length=80), nullable=False),
        sa.Column('secret_hash', sa.String(length=255), nullable=False),
        sa.Column('callback_url', sa.String(length=500), nullable=True),
        sa.Column('success_url', sa.String(length=500), nullable=True),
        sa.Column('fail_url', sa.String(length=500), nullable=True),
        sa.Column('commission_percent', sa.Numeric(8, 4), nullable=False, server_default='0.00'),
        sa.Column('min_payment_amount', sa.Numeric(18, 2), nullable=False, server_default='100.00'),
        sa.Column('max_payment_amount', sa.Numeric(18, 2), nullable=False, server_default='150000.00'),
        sa.Column('daily_limit', sa.Numeric(18, 2), nullable=True),
        sa.Column('monthly_limit', sa.Numeric(18, 2), nullable=True),
        sa.Column('balance', sa.Numeric(18, 2), nullable=False, server_default='0.00'),
        sa.Column('hold_balance', sa.Numeric(18, 2), nullable=False, server_default='0.00'),
        sa.Column('total_turnover', sa.Numeric(18, 2), nullable=False, server_default='0.00'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('api_key', name='uq_aggregator_accounts_api_key'),
        sa.UniqueConstraint('name', name='uq_aggregator_accounts_name'),
    )
    op.create_index('ix_aggregator_accounts_platform_merchant_id', 'aggregator_accounts', ['platform_merchant_id'])
    op.create_index('ix_aggregator_accounts_name', 'aggregator_accounts', ['name'])
    op.create_index('ix_aggregator_accounts_status', 'aggregator_accounts', ['status'])
    op.create_index('ix_aggregator_accounts_api_key', 'aggregator_accounts', ['api_key'])

    op.create_table(
        'aggregator_merchants',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('aggregator_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('aggregator_accounts.id', ondelete='CASCADE'), nullable=False),
        sa.Column('external_merchant_id', sa.String(length=128), nullable=False),
        sa.Column('merchant_name', sa.String(length=160), nullable=False),
        sa.Column('merchant_callback_url', sa.String(length=500), nullable=True),
        sa.Column('merchant_secret_hash', sa.String(length=255), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='active'),
        sa.Column('commission_percent', sa.Numeric(8, 4), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('aggregator_id', 'external_merchant_id', name='uq_aggregator_merchants_external'),
    )
    op.create_index('ix_aggregator_merchants_aggregator_id', 'aggregator_merchants', ['aggregator_id'])
    op.create_index('ix_aggregator_merchants_external_merchant_id', 'aggregator_merchants', ['external_merchant_id'])
    op.create_index('ix_aggregator_merchants_status', 'aggregator_merchants', ['status'])

    op.create_table(
        'aggregator_payments',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('aggregator_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('aggregator_accounts.id', ondelete='CASCADE'), nullable=False),
        sa.Column('aggregator_merchant_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('aggregator_merchants.id', ondelete='SET NULL'), nullable=True),
        sa.Column('merchant_order_id', sa.String(length=128), nullable=False),
        sa.Column('aggregator_order_id', sa.String(length=128), nullable=False),
        sa.Column('platform_payment_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('deposits.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('amount', sa.Numeric(18, 2), nullable=False),
        sa.Column('currency', sa.String(length=8), nullable=False, server_default='RUB'),
        sa.Column('payment_method', sa.String(length=32), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='created'),
        sa.Column('client_id', sa.String(length=128), nullable=True),
        sa.Column('client_ip', sa.String(length=64), nullable=True),
        sa.Column('callback_url_to_merchant', sa.String(length=500), nullable=True),
        sa.Column('callback_status', sa.String(length=32), nullable=False, server_default='pending'),
        sa.Column('callback_attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_callback_error', sa.Text(), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('metadata_json', sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('platform_payment_id', name='uq_aggregator_payments_platform_payment_id'),
        sa.UniqueConstraint('aggregator_id', 'aggregator_order_id', name='uq_aggregator_payments_aggregator_order'),
        sa.UniqueConstraint('aggregator_id', 'merchant_order_id', name='uq_aggregator_payments_merchant_order'),
    )
    op.create_index('ix_aggregator_payments_aggregator_id', 'aggregator_payments', ['aggregator_id'])
    op.create_index('ix_aggregator_payments_aggregator_merchant_id', 'aggregator_payments', ['aggregator_merchant_id'])
    op.create_index('ix_aggregator_payments_merchant_order_id', 'aggregator_payments', ['merchant_order_id'])
    op.create_index('ix_aggregator_payments_aggregator_order_id', 'aggregator_payments', ['aggregator_order_id'])
    op.create_index('ix_aggregator_payments_platform_payment_id', 'aggregator_payments', ['platform_payment_id'])
    op.create_index('ix_aggregator_payments_status', 'aggregator_payments', ['status'])
    op.create_index('ix_aggregator_payments_callback_status', 'aggregator_payments', ['callback_status'])

    op.create_table(
        'aggregator_callback_logs',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('direction', sa.String(length=32), nullable=False),
        sa.Column('related_payment_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('aggregator_payments.id', ondelete='CASCADE'), nullable=False),
        sa.Column('target_url', sa.String(length=500), nullable=False),
        sa.Column('payload_json', sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column('response_status_code', sa.Integer(), nullable=True),
        sa.Column('response_body', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='pending'),
        sa.Column('attempt', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('next_retry_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_aggregator_callback_logs_direction', 'aggregator_callback_logs', ['direction'])
    op.create_index('ix_aggregator_callback_logs_related_payment_id', 'aggregator_callback_logs', ['related_payment_id'])
    op.create_index('ix_aggregator_callback_logs_status', 'aggregator_callback_logs', ['status'])

    op.create_table(
        'aggregator_replay_nonces',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('aggregator_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('aggregator_accounts.id', ondelete='CASCADE'), nullable=False),
        sa.Column('signature', sa.String(length=128), nullable=False),
        sa.Column('request_hash', sa.String(length=64), nullable=False),
        sa.Column('timestamp', sa.Integer(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('aggregator_id', 'signature', 'request_hash', name='uq_aggregator_replay_signature_hash'),
    )
    op.create_index('ix_aggregator_replay_nonces_aggregator_id', 'aggregator_replay_nonces', ['aggregator_id'])
    op.create_index('ix_aggregator_replay_nonces_signature', 'aggregator_replay_nonces', ['signature'])
    op.create_index('ix_aggregator_replay_nonces_request_hash', 'aggregator_replay_nonces', ['request_hash'])
    op.create_index('ix_aggregator_replay_nonces_timestamp', 'aggregator_replay_nonces', ['timestamp'])
    op.create_index('ix_aggregator_replay_nonces_expires_at', 'aggregator_replay_nonces', ['expires_at'])


def downgrade():
    op.drop_index('ix_aggregator_replay_nonces_expires_at', table_name='aggregator_replay_nonces')
    op.drop_index('ix_aggregator_replay_nonces_timestamp', table_name='aggregator_replay_nonces')
    op.drop_index('ix_aggregator_replay_nonces_request_hash', table_name='aggregator_replay_nonces')
    op.drop_index('ix_aggregator_replay_nonces_signature', table_name='aggregator_replay_nonces')
    op.drop_index('ix_aggregator_replay_nonces_aggregator_id', table_name='aggregator_replay_nonces')
    op.drop_table('aggregator_replay_nonces')

    op.drop_index('ix_aggregator_callback_logs_status', table_name='aggregator_callback_logs')
    op.drop_index('ix_aggregator_callback_logs_related_payment_id', table_name='aggregator_callback_logs')
    op.drop_index('ix_aggregator_callback_logs_direction', table_name='aggregator_callback_logs')
    op.drop_table('aggregator_callback_logs')

    op.drop_index('ix_aggregator_payments_callback_status', table_name='aggregator_payments')
    op.drop_index('ix_aggregator_payments_status', table_name='aggregator_payments')
    op.drop_index('ix_aggregator_payments_platform_payment_id', table_name='aggregator_payments')
    op.drop_index('ix_aggregator_payments_aggregator_order_id', table_name='aggregator_payments')
    op.drop_index('ix_aggregator_payments_merchant_order_id', table_name='aggregator_payments')
    op.drop_index('ix_aggregator_payments_aggregator_merchant_id', table_name='aggregator_payments')
    op.drop_index('ix_aggregator_payments_aggregator_id', table_name='aggregator_payments')
    op.drop_table('aggregator_payments')

    op.drop_index('ix_aggregator_merchants_status', table_name='aggregator_merchants')
    op.drop_index('ix_aggregator_merchants_external_merchant_id', table_name='aggregator_merchants')
    op.drop_index('ix_aggregator_merchants_aggregator_id', table_name='aggregator_merchants')
    op.drop_table('aggregator_merchants')

    op.drop_index('ix_aggregator_accounts_api_key', table_name='aggregator_accounts')
    op.drop_index('ix_aggregator_accounts_status', table_name='aggregator_accounts')
    op.drop_index('ix_aggregator_accounts_name', table_name='aggregator_accounts')
    op.drop_index('ix_aggregator_accounts_platform_merchant_id', table_name='aggregator_accounts')
    op.drop_table('aggregator_accounts')
