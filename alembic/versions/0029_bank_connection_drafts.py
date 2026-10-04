"""Inactive Trader bank connection drafts, independent of static QR."""
from alembic import op
import sqlalchemy as sa

revision = "0029_bank_connection_drafts"
down_revision = "0028_static_nspk_qr"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "trader_bank_connections",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("trader_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("bank_code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("requisite_id", sa.Uuid(), sa.ForeignKey("requisites.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="not_connected"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("bank_code IN ('alfabank', 'modulbank')", name="ck_bank_draft_bank"),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_bank_draft_name"),
        sa.CheckConstraint("status = 'not_connected'", name="ck_bank_draft_inactive"),
    )
    op.create_index("ix_trader_bank_connections_trader_id", "trader_bank_connections", ["trader_id"])
    op.create_index("ix_trader_bank_connections_requisite_id", "trader_bank_connections", ["requisite_id"])


def downgrade():
    op.drop_table("trader_bank_connections")
