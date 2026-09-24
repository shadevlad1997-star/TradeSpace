"""Encrypt legacy sensitive values at rest.

Revision ID: 0013_encrypt_secrets
Revises: 0012_merchant_api_key_modes
"""

from alembic import op
import sqlalchemy as sa

from app.core.security import encrypt_secret


revision = '0013_encrypt_secrets'
down_revision = '0012_merchant_api_key_modes'
branch_labels = None
depends_on = None


SENSITIVE_COLUMNS = (
    ('users', 'twofa_secret'),
    ('api_keys', 'secret_hash'),
    ('requisites', 'value_encrypted'),
    ('aggregator_accounts', 'secret_hash'),
    ('aggregator_merchants', 'merchant_secret_hash'),
    ('kyc_profiles', 'tax_id_encrypted'),
)


def _encrypt_legacy_rows(table_name: str, column_name: str) -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(f'SELECT id, {column_name} FROM {table_name} WHERE {column_name} IS NOT NULL')
    ).mappings()
    for row in rows:
        current = row[column_name]
        if current == '':
            continue
        encrypted = encrypt_secret(current)
        if encrypted != current:
            connection.execute(
                sa.text(f'UPDATE {table_name} SET {column_name} = :value WHERE id = :id'),
                {'value': encrypted, 'id': row['id']},
            )


def upgrade() -> None:
    op.alter_column(
        'users',
        'twofa_secret',
        existing_type=sa.String(length=64),
        type_=sa.String(length=255),
        existing_nullable=True,
    )
    for table_name, column_name in SENSITIVE_COLUMNS:
        _encrypt_legacy_rows(table_name, column_name)


def downgrade() -> None:
    # The data transformation is intentionally irreversible: downgrade must never
    # put decrypted secrets back into PostgreSQL. The wider column is harmless to
    # older code and is therefore retained.
    pass
