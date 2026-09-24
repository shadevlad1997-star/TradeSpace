"""antiscam risk control

Revision ID: 0008_antiscam_risk_control
Revises: 0007_aggregator_module
Create Date: 2026-06-24
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0008_antiscam_risk_control'
down_revision = '0007_aggregator_module'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('users', sa.Column('trader_traffic_status', sa.String(length=32), nullable=False, server_default='active'))
    op.add_column('users', sa.Column('trader_risk_score', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('users', sa.Column('trader_withdrawals_frozen', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('users', sa.Column('trader_limited_until', sa.DateTime(timezone=True), nullable=True))
    op.add_column('users', sa.Column('trader_limited_max_active_payments', sa.Integer(), nullable=True))
    op.add_column('users', sa.Column('trader_limited_max_amount', sa.Numeric(18, 2), nullable=True))
    op.add_column('users', sa.Column('trader_allow_high_amount', sa.Boolean(), nullable=False, server_default=sa.true()))
    op.create_index('ix_users_trader_traffic_status', 'users', ['trader_traffic_status'])

    op.add_column('requisites', sa.Column('traffic_status', sa.String(length=32), nullable=False, server_default='active'))
    op.add_column('requisites', sa.Column('risk_score', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('requisites', sa.Column('failed_in_row', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('requisites', sa.Column('last_payment_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('requisites', sa.Column('auto_paused_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('requisites', sa.Column('limited_until', sa.DateTime(timezone=True), nullable=True))
    op.add_column('requisites', sa.Column('limited_max_active_payments', sa.Integer(), nullable=True))
    op.add_column('requisites', sa.Column('limited_max_amount', sa.Numeric(18, 2), nullable=True))
    op.add_column('requisites', sa.Column('allow_high_amount', sa.Boolean(), nullable=False, server_default=sa.true()))
    op.create_index('ix_requisites_traffic_status', 'requisites', ['traffic_status'])

    op.create_table(
        'antiscam_global_settings',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('antiscam_enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('auto_disable_traffic_enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('failed_payments_in_row_limit_requisite', sa.Integer(), nullable=False, server_default='5'),
        sa.Column('failed_payments_in_row_limit_trader', sa.Integer(), nullable=False, server_default='10'),
        sa.Column('min_payments_for_conversion_check', sa.Integer(), nullable=False, server_default='20'),
        sa.Column('conversion_check_window_minutes', sa.Integer(), nullable=False, server_default='60'),
        sa.Column('min_requisite_conversion_percent', sa.Numeric(5, 2), nullable=False, server_default='35.00'),
        sa.Column('min_trader_conversion_percent', sa.Numeric(5, 2), nullable=False, server_default='45.00'),
        sa.Column('conversion_drop_percent_limit', sa.Numeric(5, 2), nullable=False, server_default='40.00'),
        sa.Column('max_confirmation_delay_minutes', sa.Integer(), nullable=False, server_default='10'),
        sa.Column('merchant_complaints_limit_requisite', sa.Integer(), nullable=False, server_default='3'),
        sa.Column('merchant_complaints_limit_trader', sa.Integer(), nullable=False, server_default='5'),
        sa.Column('high_amount_extra_risk_enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('high_amount_threshold', sa.Numeric(18, 2), nullable=False, server_default='100000.00'),
        sa.Column('freeze_withdrawals_on_trader_auto_pause', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('default_reinstate_mode', sa.String(length=32), nullable=False, server_default='limited'),
        sa.Column('limited_reinstate_duration_minutes', sa.Integer(), nullable=False, server_default='120'),
        sa.Column('limited_reinstate_max_active_payments', sa.Integer(), nullable=False, server_default='3'),
        sa.Column('limited_reinstate_max_amount', sa.Numeric(18, 2), nullable=False, server_default='30000.00'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        'trader_antiscam_settings',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('trader_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('use_global_antiscam_settings', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('antiscam_enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('failed_payments_in_row_limit', sa.Integer(), nullable=False, server_default='10'),
        sa.Column('min_conversion_percent', sa.Numeric(5, 2), nullable=False, server_default='45.00'),
        sa.Column('conversion_check_window_minutes', sa.Integer(), nullable=False, server_default='60'),
        sa.Column('conversion_drop_percent_limit', sa.Numeric(5, 2), nullable=False, server_default='40.00'),
        sa.Column('max_confirmation_delay_minutes', sa.Integer(), nullable=False, server_default='10'),
        sa.Column('max_active_payments_when_risky', sa.Integer(), nullable=False, server_default='3'),
        sa.Column('allow_high_amount_traffic', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('risk_level', sa.String(length=32), nullable=False, server_default='strict'),
        sa.Column('auto_disable_requisites_enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('auto_disable_trader_enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('freeze_withdrawals_on_auto_pause', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('trader_id', name='uq_trader_antiscam_settings_trader_id'),
    )
    op.create_index('ix_trader_antiscam_settings_trader_id', 'trader_antiscam_settings', ['trader_id'])

    op.create_table(
        'requisite_antiscam_settings',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('requisite_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('requisites.id', ondelete='CASCADE'), nullable=False),
        sa.Column('use_global_antiscam_settings', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('antiscam_enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('failed_payments_in_row_limit', sa.Integer(), nullable=False, server_default='5'),
        sa.Column('min_conversion_percent', sa.Numeric(5, 2), nullable=False, server_default='35.00'),
        sa.Column('conversion_check_window_minutes', sa.Integer(), nullable=False, server_default='60'),
        sa.Column('max_confirmation_delay_minutes', sa.Integer(), nullable=False, server_default='10'),
        sa.Column('allow_high_amount_traffic', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('limited_max_active_payments', sa.Integer(), nullable=False, server_default='3'),
        sa.Column('limited_max_amount', sa.Numeric(18, 2), nullable=False, server_default='30000.00'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('requisite_id', name='uq_requisite_antiscam_settings_requisite_id'),
    )
    op.create_index('ix_requisite_antiscam_settings_requisite_id', 'requisite_antiscam_settings', ['requisite_id'])

    risk_event_columns = [
        sa.Column('source', sa.String(length=32), nullable=False, server_default='risk'),
        sa.Column('target_type', sa.String(length=32), nullable=True),
        sa.Column('target_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('trader_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('requisite_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('requisites.id', ondelete='SET NULL'), nullable=True),
        sa.Column('severity', sa.String(length=32), nullable=True),
        sa.Column('old_status', sa.String(length=32), nullable=True),
        sa.Column('new_status', sa.String(length=32), nullable=True),
        sa.Column('risk_score_before', sa.Integer(), nullable=True),
        sa.Column('risk_score_after', sa.Integer(), nullable=True),
        sa.Column('payments_count_in_window', sa.Integer(), nullable=True),
        sa.Column('successful_count_in_window', sa.Integer(), nullable=True),
        sa.Column('failed_count_in_window', sa.Integer(), nullable=True),
        sa.Column('conversion_before', sa.Numeric(5, 2), nullable=True),
        sa.Column('conversion_after', sa.Numeric(5, 2), nullable=True),
        sa.Column('window_minutes', sa.Integer(), nullable=True),
        sa.Column('amount_at_risk', sa.Numeric(18, 2), nullable=True),
        sa.Column('auto_action_taken', sa.String(length=64), nullable=True),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolved_by', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('resolution_status', sa.String(length=32), nullable=True),
        sa.Column('resolution_comment', sa.Text(), nullable=True),
    ]
    for column in risk_event_columns:
        op.add_column('risk_events', column)
    for index_name, columns in [
        ('ix_risk_events_source', ['source']),
        ('ix_risk_events_target_type', ['target_type']),
        ('ix_risk_events_target_id', ['target_id']),
        ('ix_risk_events_trader_id', ['trader_id']),
        ('ix_risk_events_requisite_id', ['requisite_id']),
        ('ix_risk_events_severity', ['severity']),
        ('ix_risk_events_auto_action_taken', ['auto_action_taken']),
        ('ix_risk_events_resolved_by', ['resolved_by']),
        ('ix_risk_events_resolution_status', ['resolution_status']),
    ]:
        op.create_index(index_name, 'risk_events', columns)

    op.create_table(
        'risk_decisions',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('risk_event_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('risk_events.id', ondelete='SET NULL'), nullable=True),
        sa.Column('actor_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('target_type', sa.String(length=32), nullable=False),
        sa.Column('target_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('trader_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('requisite_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('requisites.id', ondelete='SET NULL'), nullable=True),
        sa.Column('decision', sa.String(length=64), nullable=False),
        sa.Column('decision_reason', sa.Text(), nullable=False),
        sa.Column('proofs_checked', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('proof_source', sa.String(length=160), nullable=False, server_default='Telegram рабочая группа'),
        sa.Column('proof_reference', sa.String(length=500), nullable=True),
        sa.Column('reinstate_mode', sa.String(length=32), nullable=True),
        sa.Column('old_status', sa.String(length=32), nullable=True),
        sa.Column('new_status', sa.String(length=32), nullable=True),
        sa.Column('risk_score_before', sa.Integer(), nullable=True),
        sa.Column('risk_score_after', sa.Integer(), nullable=True),
        sa.Column('limited_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('max_active_payments', sa.Integer(), nullable=True),
        sa.Column('max_payment_amount', sa.Numeric(18, 2), nullable=True),
        sa.Column('allow_high_amount', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_risk_decisions_risk_event_id', 'risk_decisions', ['risk_event_id'])
    op.create_index('ix_risk_decisions_actor_id', 'risk_decisions', ['actor_id'])
    op.create_index('ix_risk_decisions_target_type', 'risk_decisions', ['target_type'])
    op.create_index('ix_risk_decisions_target_id', 'risk_decisions', ['target_id'])
    op.create_index('ix_risk_decisions_trader_id', 'risk_decisions', ['trader_id'])
    op.create_index('ix_risk_decisions_requisite_id', 'risk_decisions', ['requisite_id'])
    op.create_index('ix_risk_decisions_decision', 'risk_decisions', ['decision'])


def downgrade():
    op.drop_index('ix_risk_decisions_decision', table_name='risk_decisions')
    op.drop_index('ix_risk_decisions_requisite_id', table_name='risk_decisions')
    op.drop_index('ix_risk_decisions_trader_id', table_name='risk_decisions')
    op.drop_index('ix_risk_decisions_target_id', table_name='risk_decisions')
    op.drop_index('ix_risk_decisions_target_type', table_name='risk_decisions')
    op.drop_index('ix_risk_decisions_actor_id', table_name='risk_decisions')
    op.drop_index('ix_risk_decisions_risk_event_id', table_name='risk_decisions')
    op.drop_table('risk_decisions')

    for index_name in [
        'ix_risk_events_resolution_status',
        'ix_risk_events_resolved_by',
        'ix_risk_events_auto_action_taken',
        'ix_risk_events_severity',
        'ix_risk_events_requisite_id',
        'ix_risk_events_trader_id',
        'ix_risk_events_target_id',
        'ix_risk_events_target_type',
        'ix_risk_events_source',
    ]:
        op.drop_index(index_name, table_name='risk_events')
    for column_name in [
        'resolution_comment',
        'resolution_status',
        'resolved_by',
        'resolved_at',
        'auto_action_taken',
        'amount_at_risk',
        'window_minutes',
        'conversion_after',
        'conversion_before',
        'failed_count_in_window',
        'successful_count_in_window',
        'payments_count_in_window',
        'risk_score_after',
        'risk_score_before',
        'new_status',
        'old_status',
        'severity',
        'requisite_id',
        'trader_id',
        'target_id',
        'target_type',
        'source',
    ]:
        op.drop_column('risk_events', column_name)

    op.drop_index('ix_requisite_antiscam_settings_requisite_id', table_name='requisite_antiscam_settings')
    op.drop_table('requisite_antiscam_settings')
    op.drop_index('ix_trader_antiscam_settings_trader_id', table_name='trader_antiscam_settings')
    op.drop_table('trader_antiscam_settings')
    op.drop_table('antiscam_global_settings')

    op.drop_index('ix_requisites_traffic_status', table_name='requisites')
    op.drop_column('requisites', 'allow_high_amount')
    op.drop_column('requisites', 'limited_max_amount')
    op.drop_column('requisites', 'limited_max_active_payments')
    op.drop_column('requisites', 'limited_until')
    op.drop_column('requisites', 'auto_paused_at')
    op.drop_column('requisites', 'last_payment_at')
    op.drop_column('requisites', 'failed_in_row')
    op.drop_column('requisites', 'risk_score')
    op.drop_column('requisites', 'traffic_status')

    op.drop_index('ix_users_trader_traffic_status', table_name='users')
    op.drop_column('users', 'trader_allow_high_amount')
    op.drop_column('users', 'trader_limited_max_amount')
    op.drop_column('users', 'trader_limited_max_active_payments')
    op.drop_column('users', 'trader_limited_until')
    op.drop_column('users', 'trader_withdrawals_frozen')
    op.drop_column('users', 'trader_risk_score')
    op.drop_column('users', 'trader_traffic_status')
