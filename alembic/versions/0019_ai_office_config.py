"""Add encrypted AI Office integration configuration.

Revision ID: 0019_ai_office_config
Revises: 0018_platform_crypto_wallet
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0019_ai_office_config'
down_revision = '0018_platform_crypto_wallet'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'ai_integration_configs',
        sa.Column(
            'id',
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            'provider',
            sa.String(length=64),
            nullable=False,
            server_default='veyra_ai_office',
        ),
        sa.Column(
            'enabled',
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            'environment',
            sa.String(length=16),
            nullable=False,
            server_default='local',
        ),
        sa.Column(
            'base_url',
            sa.String(length=500),
            nullable=False,
            server_default='',
        ),
        sa.Column(
            'health_path',
            sa.String(length=255),
            nullable=False,
            server_default='/api/health',
        ),
        sa.Column('api_version', sa.String(length=64), nullable=True),
        sa.Column(
            'auth_type',
            sa.String(length=16),
            nullable=False,
            server_default='none',
        ),
        sa.Column('encrypted_api_key', sa.Text(), nullable=True),
        sa.Column('encrypted_bearer_token', sa.Text(), nullable=True),
        sa.Column('encrypted_hmac_secret', sa.Text(), nullable=True),
        sa.Column(
            'timeout_seconds',
            sa.Numeric(8, 3),
            nullable=False,
            server_default='5',
        ),
        sa.Column(
            'connect_timeout_seconds',
            sa.Numeric(8, 3),
            nullable=False,
            server_default='2',
        ),
        sa.Column(
            'max_retries',
            sa.Integer(),
            nullable=False,
            server_default='0',
        ),
        sa.Column(
            'verify_tls',
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            'selected_events',
            postgresql.JSON(),
            nullable=False,
            server_default='[]',
        ),
        sa.Column(
            'inbound_commands_enabled',
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            'last_connection_test_at',
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            'last_connection_test_status',
            sa.String(length=32),
            nullable=True,
        ),
        sa.Column(
            'last_connection_test_latency_ms',
            sa.Integer(),
            nullable=True,
        ),
        sa.Column(
            'last_success_at',
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column('last_error_code', sa.String(length=64), nullable=True),
        sa.Column(
            'last_error_message_redacted',
            sa.String(length=500),
            nullable=True,
        ),
        sa.Column(
            'created_by',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('users.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column(
            'updated_by',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('users.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            'provider',
            name='uq_ai_integration_configs_provider',
        ),
        sa.CheckConstraint(
            "provider = 'veyra_ai_office'",
            name='ck_ai_integration_config_provider',
        ),
        sa.CheckConstraint(
            "environment IN ('local','staging','production')",
            name='ck_ai_integration_config_environment',
        ),
        sa.CheckConstraint(
            "auth_type IN ('none','bearer','api_key','hmac')",
            name='ck_ai_integration_config_auth_type',
        ),
        sa.CheckConstraint(
            'timeout_seconds >= 1 AND timeout_seconds <= 60',
            name='ck_ai_integration_config_timeout',
        ),
        sa.CheckConstraint(
            'connect_timeout_seconds >= 0.1 '
            'AND connect_timeout_seconds <= timeout_seconds',
            name='ck_ai_integration_config_connect_timeout',
        ),
        sa.CheckConstraint(
            'max_retries >= 0 AND max_retries <= 5',
            name='ck_ai_integration_config_retries',
        ),
        sa.CheckConstraint(
            'inbound_commands_enabled = false',
            name='ck_ai_integration_config_no_inbound_commands',
        ),
        sa.CheckConstraint(
            "last_connection_test_status IS NULL OR "
            "last_connection_test_status IN ('success','failed')",
            name='ck_ai_integration_config_test_status',
        ),
        sa.CheckConstraint(
            'last_connection_test_latency_ms IS NULL '
            'OR last_connection_test_latency_ms >= 0',
            name='ck_ai_integration_config_latency',
        ),
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM ai_integration_configs) THEN
                RAISE EXCEPTION
                    'refusing destructive AI Office downgrade after configuration exists';
            END IF;
        END
        $$;
        """
    )
    op.drop_table('ai_integration_configs')
