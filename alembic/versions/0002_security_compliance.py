"""security and compliance hardening

Revision ID: 0002_security_compliance
Revises: 0001_initial
Create Date: 2026-04-26
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0002_security_compliance'
down_revision = '0001_initial'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('deposits', sa.Column('idempotency_key', sa.String(128), nullable=True))
    op.add_column('payouts', sa.Column('idempotency_key', sa.String(128), nullable=True))
    op.create_index('ix_deposits_idempotency_key', 'deposits', ['idempotency_key'])
    op.create_index('ix_payouts_idempotency_key', 'payouts', ['idempotency_key'])
    op.create_unique_constraint('uq_deposit_merchant_idempotency', 'deposits', ['merchant_id', 'idempotency_key'])
    op.create_unique_constraint('uq_payout_merchant_idempotency', 'payouts', ['merchant_id', 'idempotency_key'])

    op.create_table(
        'refresh_tokens',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('token_hash', sa.String(64), nullable=False, unique=True),
        sa.Column('jti', sa.String(80), nullable=False, unique=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ip', sa.String(64), nullable=True),
        sa.Column('user_agent', sa.String(300), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_refresh_tokens_user_id', 'refresh_tokens', ['user_id'])
    op.create_index('ix_refresh_tokens_token_hash', 'refresh_tokens', ['token_hash'])
    op.create_index('ix_refresh_tokens_jti', 'refresh_tokens', ['jti'])

    op.create_table(
        'requisite_usage',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('requisite_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('requisites.id', ondelete='CASCADE'), nullable=False),
        sa.Column('operation_type', sa.String(32), nullable=False),
        sa.Column('operation_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('amount', sa.Numeric(18, 2), nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_requisite_usage_requisite_id', 'requisite_usage', ['requisite_id'])
    op.create_index('ix_requisite_usage_operation_id', 'requisite_usage', ['operation_id'])

    op.create_table(
        'kyc_profiles',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('merchant_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('merchants.id', ondelete='CASCADE'), nullable=False, unique=True),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('legal_name', sa.String(255), nullable=True),
        sa.Column('tax_id_encrypted', sa.Text(), nullable=True),
        sa.Column('country', sa.String(2), nullable=False),
        sa.Column('risk_level', sa.String(32), nullable=False),
        sa.Column('reviewed_by', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_kyc_profiles_merchant_id', 'kyc_profiles', ['merchant_id'])
    op.create_index('ix_kyc_profiles_status', 'kyc_profiles', ['status'])

    op.create_table(
        'risk_events',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('merchant_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('merchants.id', ondelete='CASCADE'), nullable=True),
        sa.Column('operation_type', sa.String(32), nullable=False),
        sa.Column('operation_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('score', sa.Integer(), nullable=False),
        sa.Column('decision', sa.String(32), nullable=False),
        sa.Column('reason', sa.String(500), nullable=False),
        sa.Column('details', postgresql.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_risk_events_merchant_id', 'risk_events', ['merchant_id'])
    op.create_index('ix_risk_events_operation_type', 'risk_events', ['operation_type'])
    op.create_index('ix_risk_events_operation_id', 'risk_events', ['operation_id'])
    op.create_index('ix_risk_events_decision', 'risk_events', ['decision'])

    op.create_table(
        'report_exports',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('actor_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('report_type', sa.String(64), nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('file_path', sa.String(500), nullable=True),
        sa.Column('filters', postgresql.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_report_exports_actor_id', 'report_exports', ['actor_id'])
    op.create_index('ix_report_exports_report_type', 'report_exports', ['report_type'])


def downgrade():
    op.drop_index('ix_report_exports_report_type', table_name='report_exports')
    op.drop_index('ix_report_exports_actor_id', table_name='report_exports')
    op.drop_table('report_exports')
    op.drop_index('ix_risk_events_decision', table_name='risk_events')
    op.drop_index('ix_risk_events_operation_id', table_name='risk_events')
    op.drop_index('ix_risk_events_operation_type', table_name='risk_events')
    op.drop_index('ix_risk_events_merchant_id', table_name='risk_events')
    op.drop_table('risk_events')
    op.drop_index('ix_kyc_profiles_status', table_name='kyc_profiles')
    op.drop_index('ix_kyc_profiles_merchant_id', table_name='kyc_profiles')
    op.drop_table('kyc_profiles')
    op.drop_index('ix_requisite_usage_operation_id', table_name='requisite_usage')
    op.drop_index('ix_requisite_usage_requisite_id', table_name='requisite_usage')
    op.drop_table('requisite_usage')
    op.drop_index('ix_refresh_tokens_jti', table_name='refresh_tokens')
    op.drop_index('ix_refresh_tokens_token_hash', table_name='refresh_tokens')
    op.drop_index('ix_refresh_tokens_user_id', table_name='refresh_tokens')
    op.drop_table('refresh_tokens')
    op.drop_constraint('uq_payout_merchant_idempotency', 'payouts', type_='unique')
    op.drop_constraint('uq_deposit_merchant_idempotency', 'deposits', type_='unique')
    op.drop_index('ix_payouts_idempotency_key', table_name='payouts')
    op.drop_index('ix_deposits_idempotency_key', table_name='deposits')
    op.drop_column('payouts', 'idempotency_key')
    op.drop_column('deposits', 'idempotency_key')
