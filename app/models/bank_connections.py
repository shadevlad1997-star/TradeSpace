"""Inactive connection settings; never consulted by payment processing."""
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class TraderBankConnection(Base, TimestampMixin):
    __tablename__ = "trader_bank_connections"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    trader_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    bank_code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(120))
    requisite_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("requisites.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    status: Mapped[str] = mapped_column(String(24), default="not_connected", server_default="not_connected")
    __table_args__ = (
        CheckConstraint("bank_code IN ('alfabank', 'modulbank')", name="ck_bank_draft_bank"),
        CheckConstraint("length(trim(name)) > 0", name="ck_bank_draft_name"),
        CheckConstraint("status = 'not_connected'", name="ck_bank_draft_inactive"),
    )
