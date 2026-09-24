"""Add versioned platform USDT TRC20 wallet history.

Revision ID: 0018_platform_crypto_wallet
Revises: 0017_teamlead
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0018_platform_crypto_wallet'
down_revision = '0017_teamlead'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'platform_crypto_wallets',
        sa.Column(
            'id',
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            'asset',
            sa.String(length=16),
            nullable=False,
            server_default='USDT',
        ),
        sa.Column(
            'network',
            sa.String(length=16),
            nullable=False,
            server_default='TRC20',
        ),
        sa.Column('address', sa.String(length=128), nullable=False),
        sa.Column('label', sa.String(length=160), nullable=True),
        sa.Column(
            'is_active',
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column(
            'created_by',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('users.id', ondelete='RESTRICT'),
            nullable=False,
        ),
        sa.Column(
            'deactivated_by',
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
            'deactivated_at',
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column('change_reason', sa.String(length=500), nullable=False),
        sa.UniqueConstraint(
            'version',
            name='uq_platform_crypto_wallets_version',
        ),
        sa.CheckConstraint(
            "asset = 'USDT'",
            name='ck_platform_crypto_wallet_asset',
        ),
        sa.CheckConstraint(
            "network = 'TRC20'",
            name='ck_platform_crypto_wallet_network',
        ),
        sa.CheckConstraint(
            'version > 0',
            name='ck_platform_crypto_wallet_version',
        ),
        sa.CheckConstraint(
            '(is_active AND deactivated_at IS NULL '
            'AND deactivated_by IS NULL) OR '
            '(NOT is_active AND deactivated_at IS NOT NULL)',
            name='ck_platform_crypto_wallet_lifecycle',
        ),
    )
    op.create_index(
        'ix_platform_crypto_wallets_created_at',
        'platform_crypto_wallets',
        ['created_at'],
    )
    op.create_index(
        'uq_platform_crypto_wallet_active',
        'platform_crypto_wallets',
        ['asset', 'network'],
        unique=True,
        postgresql_where=sa.text('is_active'),
    )
    op.execute(
        """
        CREATE FUNCTION protect_platform_crypto_wallet_history()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'platform wallet history is immutable';
            END IF;
            IF OLD.is_active IS NOT TRUE
               OR NEW.is_active IS NOT FALSE
               OR NEW.deactivated_at IS NULL
               OR NEW.id IS DISTINCT FROM OLD.id
               OR NEW.asset IS DISTINCT FROM OLD.asset
               OR NEW.network IS DISTINCT FROM OLD.network
               OR NEW.address IS DISTINCT FROM OLD.address
               OR NEW.label IS DISTINCT FROM OLD.label
               OR NEW.version IS DISTINCT FROM OLD.version
               OR NEW.created_by IS DISTINCT FROM OLD.created_by
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
               OR NEW.change_reason IS DISTINCT FROM OLD.change_reason
            THEN
                RAISE EXCEPTION 'platform wallet history is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_platform_crypto_wallet_history
        BEFORE UPDATE OR DELETE ON platform_crypto_wallets
        FOR EACH ROW EXECUTE FUNCTION protect_platform_crypto_wallet_history();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM platform_crypto_wallets) THEN
                RAISE EXCEPTION
                    'refusing destructive platform wallet downgrade after wallet history exists';
            END IF;
        END
        $$;
        """
    )
    op.execute(
        'DROP TRIGGER IF EXISTS trg_platform_crypto_wallet_history '
        'ON platform_crypto_wallets'
    )
    op.execute(
        'DROP FUNCTION IF EXISTS protect_platform_crypto_wallet_history()'
    )
    op.drop_index(
        'uq_platform_crypto_wallet_active',
        table_name='platform_crypto_wallets',
    )
    op.drop_index(
        'ix_platform_crypto_wallets_created_at',
        table_name='platform_crypto_wallets',
    )
    op.drop_table('platform_crypto_wallets')
