"""Integration authorization, separate from financial accounting."""
from datetime import datetime
from uuid import UUID, uuid4
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Index, text, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base, TimestampMixin

class IntegrationEnvironment(Base):
    __tablename__ = 'integration_environment'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (CheckConstraint('id = 1', name='ck_integration_environment_singleton'),
                     CheckConstraint("mode IN ('sandbox','production')", name='ck_integration_environment_mode'))

class MerchantProductionAccess(Base, TimestampMixin):
    __tablename__ = 'merchant_production_access'
    merchant_id: Mapped[UUID] = mapped_column(ForeignKey('merchants.id', ondelete='CASCADE'), primary_key=True)
    status: Mapped[str] = mapped_column(String(16))
    changed_by: Mapped[UUID] = mapped_column(ForeignKey('users.id'))
    reason: Mapped[str] = mapped_column(String(1000))
    __table_args__ = (CheckConstraint("status IN ('active','suspended')", name='ck_merchant_production_access_status'),)


class AggregatorProductionAccess(Base, TimestampMixin):
    __tablename__ = 'aggregator_production_access'
    aggregator_id: Mapped[UUID] = mapped_column(ForeignKey('aggregator_accounts.id', ondelete='CASCADE'), primary_key=True)
    status: Mapped[str] = mapped_column(String(16))
    changed_by: Mapped[UUID] = mapped_column(ForeignKey('users.id'))
    reason: Mapped[str] = mapped_column(String(1000))
    __table_args__ = (CheckConstraint("status IN ('active','suspended')", name='ck_aggregator_production_access_status'),)


class AggregatorApiKey(Base, TimestampMixin):
    __tablename__ = 'aggregator_api_keys'
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    aggregator_id: Mapped[UUID] = mapped_column(ForeignKey('aggregator_accounts.id', ondelete='CASCADE'), index=True)
    api_key: Mapped[str] = mapped_column(String(80), unique=True)
    encrypted_secret: Mapped[str] = mapped_column(String(255))
    mode: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default='active')
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        CheckConstraint("mode IN ('sandbox','production')", name='ck_aggregator_api_key_mode'),
        CheckConstraint("status IN ('active','suspended','revoked')", name='ck_aggregator_api_key_status'),
        Index('uq_aggregator_live_key_per_mode', 'aggregator_id', 'mode', unique=True,
              postgresql_where=text("status IN ('active','suspended')")),
    )
