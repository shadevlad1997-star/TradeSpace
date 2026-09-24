"""trader controls and commission

Revision ID: 0003_trader_controls
Revises: 0002_security_compliance
Create Date: 2026-05-10
"""
from alembic import op
import sqlalchemy as sa

revision = '0003_trader_controls'
down_revision = '0002_security_compliance'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('users', sa.Column('trader_commission_percent', sa.Numeric(5, 2), nullable=False, server_default='7.00'))


def downgrade():
    op.drop_column('users', 'trader_commission_percent')