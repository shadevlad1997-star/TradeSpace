"""appeal metadata

Revision ID: 0005_appeal_metadata
Revises: 0004_merchant_settlements
Create Date: 2026-05-16
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0005_appeal_metadata'
down_revision = '0004_merchant_settlements'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('appeals', sa.Column('metadata_json', postgresql.JSON(), nullable=False, server_default='{}'))


def downgrade():
    op.drop_column('appeals', 'metadata_json')
