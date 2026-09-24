"""money safety hardening

Revision ID: 0006_money_safety_hardening
Revises: 0005_appeal_metadata
Create Date: 2026-05-28
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0006_money_safety_hardening'
down_revision = '0005_appeal_metadata'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('requisites', sa.Column('last_success_at', sa.DateTime(timezone=True), nullable=True))

    op.add_column('sms_messages', sa.Column('message_hash', sa.String(length=64), nullable=True))
    op.add_column('sms_messages', sa.Column('parse_confidence', sa.Numeric(5, 2), nullable=True))
    op.add_column('sms_messages', sa.Column('review_required', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_unique_constraint('uq_sms_messages_message_hash', 'sms_messages', ['message_hash'])

    op.add_column('webhook_events', sa.Column('last_status_code', sa.Integer(), nullable=True))
    op.add_column('webhook_events', sa.Column('response_snippet', sa.Text(), nullable=True))
    op.add_column('webhook_events', sa.Column('next_retry_at', sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        'trader_ledger_entries',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('trader_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('operation_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('entry_type', sa.String(length=64), nullable=False),
        sa.Column('amount', sa.Numeric(18, 2), nullable=False),
        sa.Column('balance_after', sa.Numeric(18, 2), nullable=False, server_default='0.00'),
        sa.Column('hold_after', sa.Numeric(18, 2), nullable=False, server_default='0.00'),
        sa.Column('currency', sa.String(length=8), nullable=False, server_default='RUB'),
        sa.Column('description', sa.String(length=500), nullable=False, server_default=''),
        sa.Column('idempotency_key', sa.String(length=160), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('idempotency_key', name='uq_trader_ledger_entries_idempotency_key'),
    )
    op.create_index('ix_trader_ledger_entries_trader_id', 'trader_ledger_entries', ['trader_id'])
    op.create_index('ix_trader_ledger_entries_operation_id', 'trader_ledger_entries', ['operation_id'])
    op.create_index('ix_trader_ledger_entries_entry_type', 'trader_ledger_entries', ['entry_type'])

    op.create_table(
        'platform_ledger_entries',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('merchant_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('merchants.id', ondelete='SET NULL'), nullable=True),
        sa.Column('operation_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('entry_type', sa.String(length=64), nullable=False),
        sa.Column('amount', sa.Numeric(18, 2), nullable=False),
        sa.Column('currency', sa.String(length=8), nullable=False, server_default='RUB'),
        sa.Column('description', sa.String(length=500), nullable=False, server_default=''),
        sa.Column('idempotency_key', sa.String(length=160), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('idempotency_key', name='uq_platform_ledger_entries_idempotency_key'),
    )
    op.create_index('ix_platform_ledger_entries_merchant_id', 'platform_ledger_entries', ['merchant_id'])
    op.create_index('ix_platform_ledger_entries_operation_id', 'platform_ledger_entries', ['operation_id'])
    op.create_index('ix_platform_ledger_entries_entry_type', 'platform_ledger_entries', ['entry_type'])

    op.create_table(
        'webhook_delivery_attempts',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('webhook_event_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('webhook_events.id', ondelete='CASCADE'), nullable=False),
        sa.Column('attempt_no', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='pending'),
        sa.Column('status_code', sa.Integer(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('response_snippet', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_webhook_delivery_attempts_webhook_event_id', 'webhook_delivery_attempts', ['webhook_event_id'])
    op.create_index('ix_webhook_delivery_attempts_status', 'webhook_delivery_attempts', ['status'])

    op.create_table(
        'api_replay_nonces',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('api_key_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('api_keys.id', ondelete='CASCADE'), nullable=False),
        sa.Column('signature', sa.String(length=128), nullable=False),
        sa.Column('request_hash', sa.String(length=64), nullable=False),
        sa.Column('timestamp', sa.Integer(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('api_key_id', 'signature', 'request_hash', name='uq_api_replay_signature_hash'),
    )
    op.create_index('ix_api_replay_nonces_api_key_id', 'api_replay_nonces', ['api_key_id'])
    op.create_index('ix_api_replay_nonces_signature', 'api_replay_nonces', ['signature'])
    op.create_index('ix_api_replay_nonces_request_hash', 'api_replay_nonces', ['request_hash'])
    op.create_index('ix_api_replay_nonces_timestamp', 'api_replay_nonces', ['timestamp'])
    op.create_index('ix_api_replay_nonces_expires_at', 'api_replay_nonces', ['expires_at'])


def downgrade():
    op.drop_index('ix_api_replay_nonces_expires_at', table_name='api_replay_nonces')
    op.drop_index('ix_api_replay_nonces_timestamp', table_name='api_replay_nonces')
    op.drop_index('ix_api_replay_nonces_request_hash', table_name='api_replay_nonces')
    op.drop_index('ix_api_replay_nonces_signature', table_name='api_replay_nonces')
    op.drop_index('ix_api_replay_nonces_api_key_id', table_name='api_replay_nonces')
    op.drop_table('api_replay_nonces')

    op.drop_index('ix_webhook_delivery_attempts_status', table_name='webhook_delivery_attempts')
    op.drop_index('ix_webhook_delivery_attempts_webhook_event_id', table_name='webhook_delivery_attempts')
    op.drop_table('webhook_delivery_attempts')

    op.drop_index('ix_platform_ledger_entries_entry_type', table_name='platform_ledger_entries')
    op.drop_index('ix_platform_ledger_entries_operation_id', table_name='platform_ledger_entries')
    op.drop_index('ix_platform_ledger_entries_merchant_id', table_name='platform_ledger_entries')
    op.drop_table('platform_ledger_entries')

    op.drop_index('ix_trader_ledger_entries_entry_type', table_name='trader_ledger_entries')
    op.drop_index('ix_trader_ledger_entries_operation_id', table_name='trader_ledger_entries')
    op.drop_index('ix_trader_ledger_entries_trader_id', table_name='trader_ledger_entries')
    op.drop_table('trader_ledger_entries')

    op.drop_column('webhook_events', 'next_retry_at')
    op.drop_column('webhook_events', 'response_snippet')
    op.drop_column('webhook_events', 'last_status_code')

    op.drop_constraint('uq_sms_messages_message_hash', 'sms_messages', type_='unique')
    op.drop_column('sms_messages', 'review_required')
    op.drop_column('sms_messages', 'parse_confidence')
    op.drop_column('sms_messages', 'message_hash')

    op.drop_column('requisites', 'last_success_at')
