"""Reconcile required lookup indexes with ORM metadata.

Revision ID: 0024_required_schema_indexes
Revises: 0023_webhook_hardening
"""

from alembic import op


revision = '0024_required_schema_indexes'
down_revision = '0023_webhook_hardening'
branch_labels = None
depends_on = None


INDEXES = (
    ('ix_api_request_logs_merchant_id', 'api_request_logs', 'merchant_id'),
    ('ix_api_request_logs_request_id', 'api_request_logs', 'request_id'),
    ('ix_appeal_messages_appeal_id', 'appeal_messages', 'appeal_id'),
    ('ix_appeals_operation_id', 'appeals', 'operation_id'),
    ('ix_audit_logs_action', 'audit_logs', 'action'),
    ('ix_audit_logs_actor_id', 'audit_logs', 'actor_id'),
    ('ix_blacklist_kind', 'blacklist', 'kind'),
    ('ix_blacklist_value', 'blacklist', 'value'),
    ('ix_deposits_external_id', 'deposits', 'external_id'),
    ('ix_fee_rules_merchant_id', 'fee_rules', 'merchant_id'),
    ('ix_ledger_entries_merchant_id', 'ledger_entries', 'merchant_id'),
    ('ix_ledger_entries_operation_id', 'ledger_entries', 'operation_id'),
    ('ix_limit_rules_merchant_id', 'limit_rules', 'merchant_id'),
    ('ix_payouts_external_id', 'payouts', 'external_id'),
    ('ix_requisites_trader_id', 'requisites', 'trader_id'),
    ('ix_webhook_events_merchant_id', 'webhook_events', 'merchant_id'),
)


def _quoted_identifier(value: str) -> str:
    # Every caller is a fixed identifier declared above. Quoting also prevents
    # accidental interpretation as a keyword if the list is extended later.
    return '"' + value.replace('"', '""') + '"'


def upgrade() -> None:
    # Lookup tables may be large in an existing installation. Concurrent builds
    # avoid blocking writes. IF NOT EXISTS makes a reviewed retry safe if a
    # previous concurrent build committed before a later build failed.
    with op.get_context().autocommit_block():
        for name, table, column in INDEXES:
            op.execute(
                'CREATE INDEX CONCURRENTLY IF NOT EXISTS '
                f'{_quoted_identifier(name)} ON '
                f'{_quoted_identifier(table)} ({_quoted_identifier(column)})'
            )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, _table, _column in reversed(INDEXES):
            op.execute(
                'DROP INDEX CONCURRENTLY IF EXISTS '
                f'{_quoted_identifier(name)}'
            )
