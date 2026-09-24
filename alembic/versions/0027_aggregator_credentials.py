"""Environment-scoped aggregator credentials and independent Production admission.
Existing credentials are retained as Sandbox only; no Production key is issued.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg
revision = '0027_aggregator_credentials'
down_revision = '0026_integration_modes'
branch_labels = None
depends_on = None


def timestamps():
    return [sa.Column(n, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
            for n in ('created_at','updated_at')]


def upgrade():
    op.create_table('aggregator_production_access',
        sa.Column('aggregator_id', pg.UUID(as_uuid=True), sa.ForeignKey('aggregator_accounts.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('changed_by', pg.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('reason', sa.String(1000), nullable=False), *timestamps(),
        sa.CheckConstraint("status IN ('active','suspended')", name='ck_aggregator_production_access_status'))
    op.create_table('aggregator_api_keys',
        sa.Column('id', pg.UUID(as_uuid=True), primary_key=True),
        sa.Column('aggregator_id', pg.UUID(as_uuid=True), sa.ForeignKey('aggregator_accounts.id', ondelete='CASCADE'), nullable=False),
        sa.Column('api_key', sa.String(80), nullable=False, unique=True),
        sa.Column('encrypted_secret', sa.String(255), nullable=False),
        sa.Column('mode', sa.String(16), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('created_by', pg.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True), *timestamps(),
        sa.CheckConstraint("mode IN ('sandbox','production')", name='ck_aggregator_api_key_mode'),
        sa.CheckConstraint("status IN ('active','suspended','revoked')", name='ck_aggregator_api_key_status'))
    op.create_index('ix_aggregator_api_keys_aggregator_id', 'aggregator_api_keys', ['aggregator_id'])
    op.create_index('uq_aggregator_live_key_per_mode', 'aggregator_api_keys', ['aggregator_id','mode'], unique=True,
                    postgresql_where=sa.text("status IN ('active','suspended')"))
    op.execute("""INSERT INTO aggregator_api_keys(id,aggregator_id,api_key,encrypted_secret,mode,status,created_at,updated_at)
        SELECT id,id,api_key,secret_hash,'sandbox','active',created_at,updated_at FROM aggregator_accounts""")


def downgrade():
    conn = op.get_bind()
    if conn.execute(sa.text('SELECT 1 FROM aggregator_api_keys LIMIT 1')).first() or conn.execute(sa.text('SELECT 1 FROM aggregator_production_access LIMIT 1')).first():
        raise RuntimeError('Cannot remove issued aggregator credentials or Production authorization')
    op.drop_table('aggregator_api_keys')
    op.drop_table('aggregator_production_access')
