"""Bind each database to one integration mode, with explicit Production approval.

On non-production deployment, all existing data remains Sandbox. Installation
in Production requires a fresh empty database, before account bootstrap.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from app.core.config import settings

revision = '0026_integration_modes'
down_revision = '0025_teamlead_merchant_referrals'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    mode = 'production' if settings.is_production else 'sandbox'
    if mode == 'production':
        for name in sa.inspect(conn).get_table_names():
            if name in {'alembic_version', 'antiscam_global_settings'}:
                continue
            table = sa.Table(name, sa.MetaData(), autoload_with=conn)
            if conn.execute(sa.select(sa.literal(1)).select_from(table).limit(1)).first():
                raise RuntimeError('Production requires a fresh empty database; existing data cannot be promoted')
    op.create_table('integration_environment',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('mode', sa.String(16), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint('id = 1', name='ck_integration_environment_singleton'),
        sa.CheckConstraint("mode IN ('sandbox','production')", name='ck_integration_environment_mode'))
    conn.execute(sa.text('INSERT INTO integration_environment (id,mode) VALUES (1,:mode)'), {'mode': mode})
    op.execute("""CREATE FUNCTION protect_integration_environment() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'Integration environment is immutable; use a separate clean database'; END $$""")
    op.execute("""CREATE TRIGGER integration_environment_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
                  ON integration_environment FOR EACH STATEMENT EXECUTE FUNCTION protect_integration_environment()""")
    op.create_table('merchant_production_access',
        sa.Column('merchant_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('merchants.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('changed_by', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('reason', sa.String(1000), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('active','suspended')", name='ck_merchant_production_access_status'))


def downgrade():
    conn = op.get_bind()
    if conn.execute(sa.text('SELECT 1 FROM merchant_production_access LIMIT 1')).first():
        raise RuntimeError('Cannot remove Production authorization after activation')
    if conn.execute(sa.text("SELECT 1 FROM integration_environment WHERE mode='production'")).first():
        raise RuntimeError('Cannot remove the Production database boundary')
    op.drop_table('merchant_production_access')
    op.drop_table('integration_environment')
    op.execute('DROP FUNCTION protect_integration_environment()')
