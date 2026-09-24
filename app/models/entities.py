import uuid
from datetime import datetime
from decimal import Decimal
from sqlalchemy import CheckConstraint, DateTime, String, Boolean, ForeignKey, Numeric, Text, UniqueConstraint, Index, Integer, JSON, event, inspect, text
from sqlalchemy.dialects.postgresql import UUID, INET
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base, TimestampMixin
from app.core.enums import (
    AppealStatus,
    DepositStatus,
    LedgerType,
    PaymentMethod,
    PayoutStatus,
    RollingAccountStatus,
    RollingAllocationStatus,
    RollingConsumptionType,
    RollingTransferStatus,
    Role,
    TeamLeadAccrualStatus,
    TeamLeadLedgerType,
    TeamLeadReferralSource,
    TeamLeadSettlementStatus,
)
from app.core.security import encrypt_secret

class User(Base, TimestampMixin):
    __tablename__='users'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str]=mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str]=mapped_column(String(255))
    role: Mapped[str]=mapped_column(String(32), default=Role.merchant.value, index=True)
    is_active: Mapped[bool]=mapped_column(Boolean, default=True)
    is_locked: Mapped[bool]=mapped_column(Boolean, default=False)
    failed_login_count: Mapped[int]=mapped_column(Integer, default=0)
    twofa_secret: Mapped[str|None]=mapped_column(String(255), nullable=True)
    twofa_enabled: Mapped[bool]=mapped_column(Boolean, default=False)
    trader_balance: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('0.00'))
    trader_hold: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('0.00'))
    trader_traffic_priority: Mapped[int]=mapped_column(Integer, default=50)
    trader_assigned_merchants: Mapped[list]=mapped_column(JSON, default=list)
    trader_commission_percent: Mapped[Decimal]=mapped_column(Numeric(5,2), default=Decimal('7.00'))
    trader_traffic_status: Mapped[str]=mapped_column(String(32), default='active', index=True)
    trader_risk_score: Mapped[int]=mapped_column(Integer, default=0)
    trader_withdrawals_frozen: Mapped[bool]=mapped_column(Boolean, default=False)
    trader_limited_until: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    trader_limited_max_active_payments: Mapped[int|None]=mapped_column(Integer, nullable=True)
    trader_limited_max_amount: Mapped[Decimal|None]=mapped_column(Numeric(18,2), nullable=True)
    trader_allow_high_amount: Mapped[bool]=mapped_column(Boolean, default=True)
    is_archived: Mapped[bool]=mapped_column(Boolean, default=False, index=True)
    archived_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    archived_reason: Mapped[str|None]=mapped_column(String(255), nullable=True)
    merchant = relationship('Merchant', back_populates='owner', uselist=False)

class Merchant(Base, TimestampMixin):
    __tablename__='merchants'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID]=mapped_column(ForeignKey('users.id', ondelete='CASCADE'), unique=True)
    name: Mapped[str]=mapped_column(String(160))
    webhook_url: Mapped[str|None]=mapped_column(String(500), nullable=True)
    ip_whitelist: Mapped[list]=mapped_column(JSON, default=list)
    sandbox_mode: Mapped[bool]=mapped_column(Boolean, default=True)
    merchant_commission_percent: Mapped[Decimal]=mapped_column(Numeric(5,2), default=Decimal('15.00'))
    is_archived: Mapped[bool]=mapped_column(Boolean, default=False, index=True)
    archived_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    owner=relationship('User', back_populates='merchant')
    balances=relationship('Balance', back_populates='merchant')
    settlements=relationship('MerchantSettlement', back_populates='merchant')
    rolling_account=relationship('MerchantRollingAccount', back_populates='merchant', uselist=False)
    rolling_transfers=relationship('MerchantRollingTransfer', back_populates='merchant')

class ApiKey(Base, TimestampMixin):
    __tablename__='api_keys'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID]=mapped_column(ForeignKey('merchants.id', ondelete='CASCADE'), index=True)
    api_key: Mapped[str]=mapped_column(String(80))
    secret_hash: Mapped[str]=mapped_column(String(255))
    is_active: Mapped[bool]=mapped_column(Boolean, default=True)
    mode: Mapped[str]=mapped_column(String(16), default='sandbox')
    last_used_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__=(
        UniqueConstraint('api_key', name='api_keys_api_key_key'),
        Index('ix_api_keys_api_key', 'api_key'),
        CheckConstraint("mode IN ('sandbox', 'production')", name='ck_api_keys_mode'),
        Index(
            'uq_api_keys_one_active_per_merchant_mode',
            'merchant_id',
            'mode',
            unique=True,
            postgresql_where=text('is_active'),
            sqlite_where=text('is_active'),
        ),
    )

class RefreshToken(Base, TimestampMixin):
    __tablename__='refresh_tokens'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID]=mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    token_hash: Mapped[str]=mapped_column(String(64))
    jti: Mapped[str]=mapped_column(String(80))
    expires_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    ip: Mapped[str|None]=mapped_column(String(64), nullable=True)
    user_agent: Mapped[str|None]=mapped_column(String(300), nullable=True)
    __table_args__=(
        UniqueConstraint('token_hash', name='refresh_tokens_token_hash_key'),
        UniqueConstraint('jti', name='refresh_tokens_jti_key'),
        Index('ix_refresh_tokens_token_hash', 'token_hash'),
        Index('ix_refresh_tokens_jti', 'jti'),
    )

class Balance(Base, TimestampMixin):
    __tablename__='balances'
    __table_args__=(UniqueConstraint('merchant_id','currency', name='uq_balance_merchant_currency'),)
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID]=mapped_column(ForeignKey('merchants.id', ondelete='CASCADE'), index=True)
    currency: Mapped[str]=mapped_column(String(8), default='RUB')
    available: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('0.00'))
    frozen: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('0.00'))
    merchant=relationship('Merchant', back_populates='balances')

class LedgerEntry(Base, TimestampMixin):
    __tablename__='ledger_entries'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID]=mapped_column(ForeignKey('merchants.id', ondelete='CASCADE'), index=True)
    operation_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    entry_type: Mapped[str]=mapped_column(String(32))
    amount: Mapped[Decimal]=mapped_column(Numeric(18,2))
    currency: Mapped[str]=mapped_column(String(8), default='RUB')
    description: Mapped[str]=mapped_column(String(500), default='')
    idempotency_key: Mapped[str|None]=mapped_column(String(128), unique=True, nullable=True)

class Deposit(Base, TimestampMixin):
    __tablename__='deposits'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID]=mapped_column(ForeignKey('merchants.id', ondelete='CASCADE'), index=True)
    external_id: Mapped[str]=mapped_column(String(128), index=True)
    idempotency_key: Mapped[str|None]=mapped_column(String(128), nullable=True, index=True)
    request_fingerprint: Mapped[str|None]=mapped_column(String(64), nullable=True)
    amount: Mapped[Decimal]=mapped_column(Numeric(18,2))
    currency: Mapped[str]=mapped_column(String(8), default='RUB')
    method: Mapped[str]=mapped_column(String(32))
    status: Mapped[str]=mapped_column(String(32), default=DepositStatus.created.value, index=True)
    requisites_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('requisites.id'), nullable=True)
    client_ip: Mapped[str|None]=mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict]=mapped_column(JSON, default=dict)
    expires_at: Mapped[datetime]=mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
        server_default=text("now() + interval '15 minutes'"),
    )
    __table_args__=(
        UniqueConstraint('merchant_id','external_id', name='uq_deposit_merchant_external'),
        UniqueConstraint('merchant_id','idempotency_key', name='uq_deposit_merchant_idempotency'),
        Index('ix_deposits_status_expires_at', 'status', 'expires_at'),
    )

class Payout(Base, TimestampMixin):
    __tablename__='payouts'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID]=mapped_column(ForeignKey('merchants.id', ondelete='CASCADE'), index=True)
    external_id: Mapped[str]=mapped_column(String(128), index=True)
    idempotency_key: Mapped[str|None]=mapped_column(String(128), nullable=True, index=True)
    request_fingerprint: Mapped[str|None]=mapped_column(String(64), nullable=True)
    amount: Mapped[Decimal]=mapped_column(Numeric(18,2))
    currency: Mapped[str]=mapped_column(String(8), default='RUB')
    method: Mapped[str]=mapped_column(String(32))
    status: Mapped[str]=mapped_column(String(32), default=PayoutStatus.created.value, index=True)
    destination: Mapped[str]=mapped_column(String(255))
    metadata_json: Mapped[dict]=mapped_column(JSON, default=dict)
    __table_args__=(
        UniqueConstraint('merchant_id','external_id', name='uq_payout_merchant_external'),
        UniqueConstraint('merchant_id','idempotency_key', name='uq_payout_merchant_idempotency'),
    )


@event.listens_for(Deposit, 'before_update')
@event.listens_for(Payout, 'before_update')
def protect_api_request_fingerprint(mapper, connection, target):
    if inspect(target).attrs.request_fingerprint.history.has_changes():
        raise ValueError('request_fingerprint is immutable')


class MerchantSettlement(Base, TimestampMixin):
    __tablename__='merchant_settlements'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID]=mapped_column(ForeignKey('merchants.id', ondelete='CASCADE'), index=True)
    requested_by_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    processed_by_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    amount_usdt: Mapped[Decimal]=mapped_column(Numeric(18,2))
    fee_usdt: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('5.00'))
    rate_rub: Mapped[Decimal]=mapped_column(Numeric(18,4), default=Decimal('100.0000'))
    amount_rub: Mapped[Decimal]=mapped_column(Numeric(18,2))
    fee_rub: Mapped[Decimal]=mapped_column(Numeric(18,2))
    total_debit_rub: Mapped[Decimal]=mapped_column(Numeric(18,2))
    trc20_address: Mapped[str]=mapped_column(String(128))
    network: Mapped[str]=mapped_column(String(32), default='TRC20')
    tx_hash: Mapped[str|None]=mapped_column(String(160), nullable=True)
    idempotency_key: Mapped[str]=mapped_column(String(180))
    rate_symbol: Mapped[str|None]=mapped_column(String(32), nullable=True)
    rate_side: Mapped[str|None]=mapped_column(String(16), nullable=True)
    rate_source: Mapped[str|None]=mapped_column(String(32), nullable=True)
    rate_provider_field: Mapped[str|None]=mapped_column(String(32), nullable=True)
    provider_timestamp: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    freshness_basis: Mapped[str|None]=mapped_column(String(32), nullable=True)
    status: Mapped[str]=mapped_column(String(32), default='pending', index=True)
    reject_reason: Mapped[str|None]=mapped_column(String(500), nullable=True)
    processed_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict]=mapped_column(JSON, default=dict)
    merchant=relationship('Merchant', back_populates='settlements')
    __table_args__=(
        UniqueConstraint(
            'merchant_id',
            'idempotency_key',
            name='uq_merchant_settlement_idempotency',
        ),
        Index(
            'uq_merchant_settlement_one_pending',
            'merchant_id',
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
        Index(
            'uq_merchant_settlement_completed_network_tx_hash',
            'network',
            'tx_hash',
            unique=True,
            postgresql_where=text(
                "status = 'completed' AND tx_hash IS NOT NULL"
            ),
            sqlite_where=text(
                "status = 'completed' AND tx_hash IS NOT NULL"
            ),
        ),
    )


@event.listens_for(MerchantSettlement, 'before_update')
def protect_merchant_settlement_quote_snapshot(mapper, connection, target):
    del mapper, connection
    immutable_fields = {
        'merchant_id',
        'requested_by_id',
        'amount_usdt',
        'fee_usdt',
        'rate_rub',
        'amount_rub',
        'fee_rub',
        'total_debit_rub',
        'trc20_address',
        'network',
        'idempotency_key',
        'rate_symbol',
        'rate_side',
        'rate_source',
        'rate_provider_field',
        'provider_timestamp',
        'fetched_at',
        'freshness_basis',
        'metadata_json',
    }
    changed = sorted(
        field
        for field in immutable_fields
        if inspect(target).attrs[field].history.has_changes()
    )
    if changed:
        raise ValueError(
            'merchant settlement snapshot is immutable: '
            + ', '.join(changed)
        )


class TraderLedgerEntry(Base, TimestampMixin):
    __tablename__='trader_ledger_entries'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trader_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), index=True)
    operation_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    entry_type: Mapped[str]=mapped_column(String(64), index=True)
    amount: Mapped[Decimal]=mapped_column(Numeric(18,2))
    balance_after: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('0.00'))
    hold_after: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('0.00'))
    currency: Mapped[str]=mapped_column(String(8), default='RUB')
    description: Mapped[str]=mapped_column(String(500), default='')
    idempotency_key: Mapped[str|None]=mapped_column(String(160), unique=True, nullable=True)


class PlatformLedgerEntry(Base, TimestampMixin):
    __tablename__='platform_ledger_entries'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('merchants.id', ondelete='SET NULL'), nullable=True, index=True)
    operation_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    entry_type: Mapped[str]=mapped_column(String(64), index=True)
    amount: Mapped[Decimal]=mapped_column(Numeric(18,2))
    currency: Mapped[str]=mapped_column(String(8), default='RUB')
    description: Mapped[str]=mapped_column(String(500), default='')
    idempotency_key: Mapped[str|None]=mapped_column(String(160), unique=True, nullable=True)


class TeamLeadTraderAssignment(Base, TimestampMixin):
    __tablename__ = 'teamlead_trader_assignments'
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    teamlead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey('users.id', ondelete='RESTRICT'), index=True
    )
    trader_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey('users.id', ondelete='RESTRICT'), index=True
    )
    commission_percent: Mapped[Decimal] = mapped_column(Numeric(10, 6))
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey('users.id', ondelete='SET NULL'), nullable=True
    )
    closed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey('users.id', ondelete='SET NULL'), nullable=True
    )
    creation_reason: Mapped[str] = mapped_column(String(500))
    close_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    __table_args__ = (
        CheckConstraint(
            'commission_percent >= 0 AND commission_percent <= 100',
            name='ck_teamlead_assignment_percent',
        ),
        CheckConstraint(
            'effective_to IS NULL OR effective_to > effective_from',
            name='ck_teamlead_assignment_effective_period',
        ),
        Index(
            'uq_teamlead_assignment_active_trader',
            'trader_id',
            unique=True,
            postgresql_where=text('effective_to IS NULL'),
            sqlite_where=text('effective_to IS NULL'),
        ),
    )


@event.listens_for(TeamLeadTraderAssignment, 'before_update')
def protect_teamlead_assignment_history(mapper, connection, target):
    allowed = {'effective_to', 'closed_by', 'close_reason', 'updated_at'}
    changed = {
        attribute.key
        for attribute in inspect(target).attrs
        if attribute.history.has_changes()
    }
    forbidden = changed.difference(allowed)
    if forbidden:
        raise ValueError(
            'teamlead assignment history is immutable: '
            + ', '.join(sorted(forbidden))
        )
    effective_history = inspect(target).attrs.effective_to.history
    previous = effective_history.deleted[0] if effective_history.deleted else None
    closing_now = (
        'effective_to' in changed
        and previous is None
        and target.effective_to is not None
    )
    if target.effective_to is not None and not closing_now:
        raise ValueError('closed teamlead assignment is immutable')


class TeamLeadMerchantAssignment(Base, TimestampMixin):
    __tablename__ = 'teamlead_merchant_assignments'
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    teamlead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey('users.id', ondelete='RESTRICT'), index=True
    )
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey('merchants.id', ondelete='RESTRICT'), index=True
    )
    commission_percent: Mapped[Decimal] = mapped_column(Numeric(10, 6))
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey('users.id', ondelete='SET NULL'), nullable=True
    )
    closed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey('users.id', ondelete='SET NULL'), nullable=True
    )
    reason: Mapped[str] = mapped_column(String(500))
    close_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    __table_args__ = (
        CheckConstraint(
            'commission_percent >= 0 AND commission_percent <= 100',
            name='ck_teamlead_merchant_assignment_percent',
        ),
        CheckConstraint(
            'valid_to IS NULL OR valid_to > valid_from',
            name='ck_teamlead_merchant_assignment_period',
        ),
        Index(
            'uq_teamlead_merchant_assignment_active_merchant',
            'merchant_id',
            unique=True,
            postgresql_where=text('valid_to IS NULL'),
            sqlite_where=text('valid_to IS NULL'),
        ),
    )


@event.listens_for(TeamLeadMerchantAssignment, 'before_update')
def protect_teamlead_merchant_assignment_history(mapper, connection, target):
    allowed = {'valid_to', 'closed_by', 'close_reason', 'updated_at'}
    changed = {
        attribute.key
        for attribute in inspect(target).attrs
        if attribute.history.has_changes()
    }
    forbidden = changed.difference(allowed)
    if forbidden:
        raise ValueError(
            'teamlead merchant assignment history is immutable: '
            + ', '.join(sorted(forbidden))
        )
    valid_to_history = inspect(target).attrs.valid_to.history
    previous = valid_to_history.deleted[0] if valid_to_history.deleted else None
    closing_now = (
        'valid_to' in changed
        and previous is None
        and target.valid_to is not None
    )
    if target.valid_to is not None and not closing_now:
        raise ValueError('closed teamlead merchant assignment is immutable')


class TeamLeadBalance(Base, TimestampMixin):
    __tablename__ = 'teamlead_balances'
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    teamlead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey('users.id', ondelete='RESTRICT'), index=True
    )
    available_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal('0.00'))
    frozen_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal('0.00'))
    debt_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal('0.00'))
    total_earned_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal('0.00'))
    total_paid_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal('0.00'))
    __table_args__ = (
        UniqueConstraint(
            'teamlead_id',
            name='uq_teamlead_balances_teamlead_id',
        ),
        CheckConstraint('available_rub >= 0', name='ck_teamlead_balance_available'),
        CheckConstraint('frozen_rub >= 0', name='ck_teamlead_balance_frozen'),
        CheckConstraint('debt_rub >= 0', name='ck_teamlead_balance_debt'),
        CheckConstraint('total_earned_rub >= 0', name='ck_teamlead_balance_earned'),
        CheckConstraint('total_paid_rub >= 0', name='ck_teamlead_balance_paid'),
    )


class TeamLeadAccrual(Base, TimestampMixin):
    __tablename__ = 'teamlead_accruals'
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    teamlead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey('users.id', ondelete='RESTRICT'), index=True
    )
    trader_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey('users.id', ondelete='RESTRICT'), index=True
    )
    assignment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey('teamlead_trader_assignments.id', ondelete='RESTRICT'), index=True
    )
    deposit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey('deposits.id', ondelete='RESTRICT'), index=True
    )
    gross_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    commission_percent_snapshot: Mapped[Decimal] = mapped_column(Numeric(10, 6))
    accrual_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    credited_to_available_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    applied_to_debt_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    status: Mapped[str] = mapped_column(
        String(16), default=TeamLeadAccrualStatus.credited.value, index=True
    )
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reversal_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    __table_args__ = (
        UniqueConstraint(
            'deposit_id',
            name='uq_teamlead_accruals_deposit_id',
        ),
        CheckConstraint('gross_rub >= 0', name='ck_teamlead_accrual_gross'),
        CheckConstraint(
            'commission_percent_snapshot >= 0 AND commission_percent_snapshot <= 100',
            name='ck_teamlead_accrual_percent',
        ),
        CheckConstraint('accrual_rub >= 0', name='ck_teamlead_accrual_amount'),
        CheckConstraint('credited_to_available_rub >= 0', name='ck_teamlead_accrual_credited'),
        CheckConstraint('applied_to_debt_rub >= 0', name='ck_teamlead_accrual_debt_offset'),
        CheckConstraint(
            'credited_to_available_rub + applied_to_debt_rub = accrual_rub',
            name='ck_teamlead_accrual_distribution',
        ),
        CheckConstraint(
            "status IN ('credited','reversed')",
            name='ck_teamlead_accrual_status',
        ),
    )


@event.listens_for(TeamLeadAccrual, 'before_update')
def protect_teamlead_accrual_history(mapper, connection, target):
    allowed = {'status', 'reversed_at', 'reversal_reason', 'updated_at'}
    changed = {
        attribute.key
        for attribute in inspect(target).attrs
        if attribute.history.has_changes()
    }
    forbidden = changed.difference(allowed)
    if forbidden:
        raise ValueError(
            'teamlead accrual snapshot is immutable: '
            + ', '.join(sorted(forbidden))
        )
    status_history = inspect(target).attrs.status.history
    previous = status_history.deleted[0] if status_history.deleted else None
    reversing_now = (
        'status' in changed
        and previous == TeamLeadAccrualStatus.credited.value
        and target.status == TeamLeadAccrualStatus.reversed.value
    )
    if changed.difference({'updated_at'}) and not reversing_now:
        raise ValueError('reversed teamlead accrual is immutable')


class TeamLeadMerchantAccrual(Base, TimestampMixin):
    __tablename__ = 'teamlead_merchant_accruals'
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    teamlead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey('users.id', ondelete='RESTRICT'), index=True
    )
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey('merchants.id', ondelete='RESTRICT'), index=True
    )
    assignment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey('teamlead_merchant_assignments.id', ondelete='RESTRICT'), index=True
    )
    deposit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey('deposits.id', ondelete='RESTRICT'), index=True
    )
    source_type: Mapped[str] = mapped_column(
        String(32), default=TeamLeadReferralSource.merchant_referral.value
    )
    gross_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    commission_percent_snapshot: Mapped[Decimal] = mapped_column(Numeric(10, 6))
    accrual_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    credited_to_available_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    applied_to_debt_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    status: Mapped[str] = mapped_column(
        String(16), default=TeamLeadAccrualStatus.credited.value, index=True
    )
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reversal_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    __table_args__ = (
        UniqueConstraint(
            'deposit_id',
            'source_type',
            name='uq_teamlead_merchant_accrual_deposit_source',
        ),
        CheckConstraint(
            "source_type = 'merchant_referral'",
            name='ck_teamlead_merchant_accrual_source',
        ),
        CheckConstraint('gross_rub >= 0', name='ck_teamlead_merchant_accrual_gross'),
        CheckConstraint(
            'commission_percent_snapshot >= 0 AND commission_percent_snapshot <= 100',
            name='ck_teamlead_merchant_accrual_percent',
        ),
        CheckConstraint('accrual_rub >= 0', name='ck_teamlead_merchant_accrual_amount'),
        CheckConstraint(
            'credited_to_available_rub >= 0',
            name='ck_teamlead_merchant_accrual_credited',
        ),
        CheckConstraint(
            'applied_to_debt_rub >= 0',
            name='ck_teamlead_merchant_accrual_debt_offset',
        ),
        CheckConstraint(
            'credited_to_available_rub + applied_to_debt_rub = accrual_rub',
            name='ck_teamlead_merchant_accrual_distribution',
        ),
        CheckConstraint(
            "status IN ('credited','reversed')",
            name='ck_teamlead_merchant_accrual_status',
        ),
    )


@event.listens_for(TeamLeadMerchantAccrual, 'before_update')
def protect_teamlead_merchant_accrual_history(mapper, connection, target):
    allowed = {'status', 'reversed_at', 'reversal_reason', 'updated_at'}
    changed = {
        attribute.key
        for attribute in inspect(target).attrs
        if attribute.history.has_changes()
    }
    forbidden = changed.difference(allowed)
    if forbidden:
        raise ValueError(
            'teamlead merchant accrual snapshot is immutable: '
            + ', '.join(sorted(forbidden))
        )
    status_history = inspect(target).attrs.status.history
    previous = status_history.deleted[0] if status_history.deleted else None
    reversing_now = (
        'status' in changed
        and previous == TeamLeadAccrualStatus.credited.value
        and target.status == TeamLeadAccrualStatus.reversed.value
    )
    if changed.difference({'updated_at'}) and not reversing_now:
        raise ValueError('reversed teamlead merchant accrual is immutable')


class TeamLeadSettlement(Base, TimestampMixin):
    __tablename__ = 'teamlead_settlements'
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    teamlead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey('users.id', ondelete='RESTRICT'), index=True
    )
    requested_usdt: Mapped[Decimal] = mapped_column(Numeric(24, 6))
    fee_usdt: Mapped[Decimal] = mapped_column(Numeric(24, 6), default=Decimal('5.000000'))
    total_debit_usdt: Mapped[Decimal] = mapped_column(Numeric(24, 6))
    rapira_rate_rub: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    rate_symbol: Mapped[str] = mapped_column(String(16))
    rate_source: Mapped[str] = mapped_column(String(64))
    rate_side: Mapped[str] = mapped_column(String(8), default='ask')
    provider_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    freshness_basis: Mapped[str] = mapped_column(String(32))
    requested_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    fee_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    total_debit_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    network: Mapped[str] = mapped_column(String(16), default='TRC20')
    wallet_address: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(
        String(16), default=TeamLeadSettlementStatus.pending.value, index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(180), unique=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey('users.id', ondelete='SET NULL'), nullable=True
    )
    tx_hash: Mapped[str | None] = mapped_column(String(160), nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    __table_args__ = (
        CheckConstraint('requested_usdt > 0', name='ck_teamlead_settlement_requested'),
        CheckConstraint("fee_usdt = 5.000000", name='ck_teamlead_settlement_fee'),
        CheckConstraint(
            'total_debit_usdt = requested_usdt + fee_usdt',
            name='ck_teamlead_settlement_total_usdt',
        ),
        CheckConstraint(
            'total_debit_rub = requested_rub + fee_rub',
            name='ck_teamlead_settlement_total_rub',
        ),
        CheckConstraint('rapira_rate_rub > 0', name='ck_teamlead_settlement_rate'),
        CheckConstraint("rate_side = 'ask'", name='ck_teamlead_settlement_side'),
        CheckConstraint("rate_source = 'rapira_live'", name='ck_teamlead_settlement_source'),
        CheckConstraint(
            "freshness_basis IN ('provider_timestamp','fetched_at')",
            name='ck_teamlead_settlement_freshness',
        ),
        CheckConstraint("network = 'TRC20'", name='ck_teamlead_settlement_network'),
        CheckConstraint(
            "status IN ('pending','completed','rejected')",
            name='ck_teamlead_settlement_status',
        ),
        CheckConstraint(
            "(status = 'pending' AND completed_at IS NULL "
            "AND rejected_at IS NULL AND tx_hash IS NULL "
            "AND reject_reason IS NULL) OR "
            "(status = 'completed' AND completed_at IS NOT NULL "
            "AND rejected_at IS NULL AND tx_hash IS NOT NULL "
            "AND trim(tx_hash) <> '' AND reject_reason IS NULL) OR "
            "(status = 'rejected' AND rejected_at IS NOT NULL "
            "AND completed_at IS NULL AND tx_hash IS NULL "
            "AND reject_reason IS NOT NULL AND trim(reject_reason) <> '')",
            name='ck_teamlead_settlement_state',
        ),
        Index(
            'uq_teamlead_settlement_pending',
            'teamlead_id',
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
        Index(
            'uq_teamlead_settlement_completed_tx',
            'network',
            'tx_hash',
            unique=True,
            postgresql_where=text("status = 'completed' AND tx_hash IS NOT NULL"),
            sqlite_where=text("status = 'completed' AND tx_hash IS NOT NULL"),
        ),
    )


@event.listens_for(TeamLeadSettlement, 'before_update')
def protect_teamlead_settlement_snapshot(mapper, connection, target):
    allowed = {
        'status',
        'completed_at',
        'rejected_at',
        'processed_by',
        'tx_hash',
        'reject_reason',
        'updated_at',
    }
    changed = {
        attribute.key
        for attribute in inspect(target).attrs
        if attribute.history.has_changes()
    }
    forbidden = changed.difference(allowed)
    if forbidden:
        raise ValueError(
            'teamlead settlement snapshot is immutable: '
            + ', '.join(sorted(forbidden))
        )
    status_history = inspect(target).attrs.status.history
    previous = status_history.deleted[0] if status_history.deleted else None
    finalizing_now = (
        'status' in changed
        and previous == TeamLeadSettlementStatus.pending.value
        and target.status in {
            TeamLeadSettlementStatus.completed.value,
            TeamLeadSettlementStatus.rejected.value,
        }
    )
    if changed.difference({'updated_at'}) and not finalizing_now:
        raise ValueError('final teamlead settlement is immutable')


class TeamLeadLedgerEntry(Base, TimestampMixin):
    __tablename__ = 'teamlead_ledger_entries'
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    teamlead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey('users.id', ondelete='RESTRICT'), index=True
    )
    accrual_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey('teamlead_accruals.id', ondelete='RESTRICT'), nullable=True
    )
    merchant_accrual_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey('teamlead_merchant_accruals.id', ondelete='RESTRICT'), nullable=True, index=True
    )
    settlement_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey('teamlead_settlements.id', ondelete='RESTRICT'), nullable=True
    )
    deposit_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey('deposits.id', ondelete='RESTRICT'), nullable=True
    )
    merchant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey('merchants.id', ondelete='RESTRICT'), nullable=True, index=True
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey('users.id', ondelete='SET NULL'), nullable=True
    )
    entry_type: Mapped[str] = mapped_column(String(32), index=True)
    source_type: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    amount_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    available_after: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    frozen_after: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    debt_after: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    idempotency_key: Mapped[str] = mapped_column(String(180), unique=True)
    reason: Mapped[str] = mapped_column(String(500))
    __table_args__ = (
        CheckConstraint('amount_rub >= 0', name='ck_teamlead_ledger_amount'),
        CheckConstraint('available_after >= 0', name='ck_teamlead_ledger_available'),
        CheckConstraint('frozen_after >= 0', name='ck_teamlead_ledger_frozen'),
        CheckConstraint('debt_after >= 0', name='ck_teamlead_ledger_debt'),
        CheckConstraint(
            "source_type IS NULL OR source_type IN ('trader_referral','merchant_referral')",
            name='ck_teamlead_ledger_source_type',
        ),
        CheckConstraint(
            "entry_type IN ('accrual_credit','debt_offset','accrual_reversal',"
            "'settlement_freeze','settlement_release','settlement_complete',"
            "'manual_adjustment','write_off')",
            name='ck_teamlead_ledger_type',
        ),
        Index(
            'ix_teamlead_ledger_teamlead_created',
            'teamlead_id',
            'created_at',
        ),
    )


@event.listens_for(TeamLeadLedgerEntry, 'before_update')
@event.listens_for(TeamLeadLedgerEntry, 'before_delete')
def protect_teamlead_ledger(mapper, connection, target):
    raise ValueError('teamlead ledger entries are immutable')


class MerchantRollingAccount(Base, TimestampMixin):
    __tablename__ = 'merchant_rolling_accounts'
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey('merchants.id', ondelete='CASCADE')
    )
    principal_usdt: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), default=Decimal('0.000000')
    )
    recovered_usdt: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), default=Decimal('0.000000')
    )
    outstanding_usdt: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), default=Decimal('0.000000')
    )
    status: Mapped[str] = mapped_column(
        String(16), default=RollingAccountStatus.exhausted.value, index=True
    )
    merchant = relationship('Merchant', back_populates='rolling_account')
    __table_args__ = (
        UniqueConstraint(
            'merchant_id',
            name='uq_merchant_rolling_accounts_merchant_id',
        ),
        CheckConstraint('principal_usdt >= 0', name='ck_rolling_account_principal_nonnegative'),
        CheckConstraint('recovered_usdt >= 0', name='ck_rolling_account_recovered_nonnegative'),
        CheckConstraint('outstanding_usdt >= 0', name='ck_rolling_account_outstanding_nonnegative'),
        CheckConstraint(
            "status IN ('active','exhausted','suspended')",
            name='ck_rolling_account_status',
        ),
    )


class MerchantRollingTransfer(Base, TimestampMixin):
    __tablename__ = 'merchant_rolling_transfers'
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('merchants.id', ondelete='RESTRICT'),
        index=True,
    )
    rolling_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('merchant_rolling_accounts.id', ondelete='RESTRICT'),
        nullable=True,
        index=True,
    )
    sequence_no: Mapped[int] = mapped_column(Integer)
    amount_usdt: Mapped[Decimal] = mapped_column(Numeric(24, 6))
    recovered_usdt: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), default=Decimal('0.000000')
    )
    remaining_usdt: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), default=Decimal('0.000000')
    )
    network: Mapped[str | None] = mapped_column(String(32), nullable=True)
    destination_address: Mapped[str | None] = mapped_column(
        String(160), nullable=True
    )
    tx_hash: Mapped[str | None] = mapped_column(String(160), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32),
        default=RollingTransferStatus.pending_confirmation.value,
        index=True,
    )
    source: Mapped[str] = mapped_column(String(32), default='registered')
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    disputed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True,
    )
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True,
    )
    disputed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True,
    )
    cancelled_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True,
    )
    comment: Mapped[str | None] = mapped_column(String(500), nullable=True)
    dispute_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(180), unique=True)
    merchant = relationship('Merchant', back_populates='rolling_transfers')
    __table_args__ = (
        UniqueConstraint(
            'merchant_id',
            'sequence_no',
            name='uq_rolling_transfer_merchant_sequence',
        ),
        UniqueConstraint(
            'network',
            'tx_hash',
            name='uq_rolling_transfer_network_tx_hash',
        ),
        CheckConstraint(
            'amount_usdt > 0',
            name='ck_rolling_transfer_amount_positive',
        ),
        CheckConstraint(
            'recovered_usdt >= 0',
            name='ck_rolling_transfer_recovered_nonnegative',
        ),
        CheckConstraint(
            'remaining_usdt >= 0',
            name='ck_rolling_transfer_remaining_nonnegative',
        ),
        CheckConstraint(
            "(status = 'confirmed' "
            "AND rolling_account_id IS NOT NULL "
            "AND confirmed_at IS NOT NULL "
            "AND recovered_usdt + remaining_usdt = amount_usdt) "
            "OR (status IN ('pending_confirmation','disputed','cancelled') "
            "AND rolling_account_id IS NULL "
            "AND confirmed_at IS NULL "
            "AND recovered_usdt = 0 "
            "AND remaining_usdt = 0)",
            name='ck_rolling_transfer_financial_state',
        ),
        CheckConstraint(
            "(source = 'legacy_migration' AND tx_hash IS NULL) "
            "OR (source = 'registered' AND network IS NOT NULL "
            "AND destination_address IS NOT NULL AND tx_hash IS NOT NULL)",
            name='ck_rolling_transfer_evidence',
        ),
        CheckConstraint(
            "status IN ('pending_confirmation','confirmed','disputed','cancelled')",
            name='ck_rolling_transfer_status',
        ),
        CheckConstraint(
            "source IN ('registered','legacy_migration')",
            name='ck_rolling_transfer_source',
        ),
        Index(
            'ix_rolling_transfer_merchant_status_sequence',
            'merchant_id',
            'status',
            'sequence_no',
        ),
    )


class MerchantRollingAllocation(Base, TimestampMixin):
    __tablename__ = 'merchant_rolling_allocations'
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    deposit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey('deposits.id', ondelete='RESTRICT')
    )
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey('merchants.id', ondelete='RESTRICT'), index=True
    )
    rolling_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey('merchant_rolling_accounts.id', ondelete='RESTRICT'), index=True
    )
    gross_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    merchant_fee_percent_snapshot: Mapped[Decimal] = mapped_column(Numeric(10, 6))
    merchant_fee_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    merchant_payable_rub: Mapped[Decimal] = mapped_column(
        'merchant_net_rub', Numeric(18, 2)
    )
    rapira_rate_rub: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    rapira_rate_symbol: Mapped[str] = mapped_column(String(16))
    rapira_rate_side: Mapped[str] = mapped_column(String(8), default='ask')
    rapira_rate_source: Mapped[str] = mapped_column(String(64))
    rapira_rate_field: Mapped[str] = mapped_column(String(64), default='askPrice')
    rapira_rate_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    rapira_provider_timestamp: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rapira_fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    rapira_freshness_basis: Mapped[str] = mapped_column(String(32))
    merchant_payable_usdt: Mapped[Decimal] = mapped_column(
        'merchant_net_usdt', Numeric(24, 6)
    )
    eligibility_status: Mapped[str] = mapped_column(
        String(16), default='eligible'
    )
    eligible_transfer_sequence: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    rolling_eligible_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    eligibility_source: Mapped[str] = mapped_column(
        String(32), default='confirmed_transfer'
    )
    rolling_applied_usdt: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), default=Decimal('0.000000')
    )
    rolling_applied_rub: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), default=Decimal('0.00')
    )
    settle_credited_rub: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), default=Decimal('0.00')
    )
    status: Mapped[str] = mapped_column(
        String(16), default=RollingAllocationStatus.pending.value, index=True
    )
    release_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint(
            'deposit_id',
            name='uq_merchant_rolling_allocations_deposit_id',
        ),
        CheckConstraint('gross_rub >= 0', name='ck_rolling_allocation_gross_nonnegative'),
        CheckConstraint('merchant_fee_rub >= 0', name='ck_rolling_allocation_fee_nonnegative'),
        CheckConstraint('merchant_net_rub >= 0', name='ck_rolling_allocation_net_rub_nonnegative'),
        CheckConstraint('merchant_net_usdt >= 0', name='ck_rolling_allocation_net_usdt_nonnegative'),
        CheckConstraint('rolling_applied_usdt >= 0', name='ck_rolling_allocation_applied_usdt_nonnegative'),
        CheckConstraint('rolling_applied_rub >= 0', name='ck_rolling_allocation_applied_rub_nonnegative'),
        CheckConstraint('settle_credited_rub >= 0', name='ck_rolling_allocation_settle_nonnegative'),
        CheckConstraint("rapira_rate_side = 'ask'", name='ck_rolling_allocation_rate_side'),
        CheckConstraint(
            "rapira_freshness_basis IN ('provider_timestamp','fetched_at')",
            name='ck_rolling_allocation_freshness_basis',
        ),
        CheckConstraint(
            "status IN ('pending','paid','released','reversed')",
            name='ck_rolling_allocation_status',
        ),
        CheckConstraint(
            "(eligibility_status = 'eligible' "
            "AND eligible_transfer_sequence > 0 "
            "AND rolling_eligible_at IS NOT NULL "
            "AND eligibility_source IN "
            "('confirmed_transfer','legacy_migration')) "
            "OR (eligibility_status = 'ineligible' "
            "AND eligible_transfer_sequence IS NULL "
            "AND rolling_eligible_at IS NULL "
            "AND eligibility_source IN "
            "('legacy_no_confirmed_funding',"
            "'no_confirmed_funding_at_creation'))",
            name='ck_rolling_allocation_eligibility_state',
        ),
        Index(
            'ix_merchant_rolling_allocations_merchant_status',
            'merchant_id',
            'status',
        ),
    )


class MerchantRollingLedgerEntry(Base, TimestampMixin):
    __tablename__ = 'merchant_rolling_ledger_entries'
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    rolling_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey('merchant_rolling_accounts.id', ondelete='RESTRICT'), index=True
    )
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey('merchants.id', ondelete='RESTRICT'), index=True
    )
    deposit_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey('deposits.id', ondelete='RESTRICT'), nullable=True, index=True
    )
    rolling_transfer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('merchant_rolling_transfers.id', ondelete='RESTRICT'),
        nullable=True,
        index=True,
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True
    )
    entry_type: Mapped[str] = mapped_column(String(32), index=True)
    amount_usdt: Mapped[Decimal | None] = mapped_column(Numeric(24, 6), nullable=True)
    amount_rub: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    rate_rub: Mapped[Decimal | None] = mapped_column(Numeric(24, 8), nullable=True)
    network: Mapped[str | None] = mapped_column(String(32), nullable=True)
    destination_address: Mapped[str | None] = mapped_column(String(160), nullable=True)
    tx_hash: Mapped[str | None] = mapped_column(String(160), nullable=True)
    funded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str] = mapped_column(String(500))
    idempotency_key: Mapped[str] = mapped_column(String(180), unique=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (
        CheckConstraint(
            "entry_type IN ('funding','topup','pending_added','pending_released',"
            "'recovery','settle_overflow','reversal','manual_adjustment','write_off')",
            name='ck_rolling_ledger_entry_type',
        ),
        CheckConstraint(
            'amount_usdt IS NULL OR amount_usdt >= 0',
            name='ck_rolling_ledger_amount_usdt_nonnegative',
        ),
        CheckConstraint(
            'amount_rub IS NULL OR amount_rub >= 0',
            name='ck_rolling_ledger_amount_rub_nonnegative',
        ),
        Index(
            'ix_merchant_rolling_ledger_account_created',
            'rolling_account_id',
            'created_at',
        ),
        Index(
            'uq_merchant_rolling_ledger_network_tx_hash',
            'network',
            'tx_hash',
            unique=True,
            postgresql_where=text(
                "entry_type IN ('funding','topup') AND network IS NOT NULL AND tx_hash IS NOT NULL"
            ),
        ),
    )


class MerchantRollingTransferConsumption(Base, TimestampMixin):
    __tablename__ = 'merchant_rolling_transfer_consumptions'
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('merchants.id', ondelete='RESTRICT'),
        index=True,
    )
    rolling_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('merchant_rolling_accounts.id', ondelete='RESTRICT'),
        index=True,
    )
    rolling_transfer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('merchant_rolling_transfers.id', ondelete='RESTRICT'),
        index=True,
    )
    rolling_allocation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('merchant_rolling_allocations.id', ondelete='RESTRICT'),
        index=True,
    )
    deposit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('deposits.id', ondelete='RESTRICT'),
        index=True,
    )
    entry_type: Mapped[str] = mapped_column(String(16))
    amount_usdt: Mapped[Decimal] = mapped_column(Numeric(24, 6))
    amount_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    rate_rub: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    idempotency_key: Mapped[str] = mapped_column(String(180), unique=True)
    reason: Mapped[str] = mapped_column(String(500))
    __table_args__ = (
        CheckConstraint(
            "entry_type IN ('recovery','reversal')",
            name='ck_rolling_consumption_entry_type',
        ),
        CheckConstraint(
            'amount_usdt > 0',
            name='ck_rolling_consumption_amount_usdt_positive',
        ),
        CheckConstraint(
            'amount_rub >= 0',
            name='ck_rolling_consumption_amount_rub_nonnegative',
        ),
        CheckConstraint(
            'rate_rub > 0',
            name='ck_rolling_consumption_rate_positive',
        ),
        UniqueConstraint(
            'rolling_transfer_id',
            'deposit_id',
            'entry_type',
            name='uq_rolling_consumption_transfer_deposit_type',
        ),
        Index(
            'ix_rolling_consumption_transfer_created',
            'rolling_transfer_id',
            'created_at',
        ),
    )


@event.listens_for(MerchantRollingLedgerEntry, 'before_update')
@event.listens_for(MerchantRollingLedgerEntry, 'before_delete')
def protect_rolling_ledger(mapper, connection, target):
    raise ValueError('rolling ledger entries are immutable')


@event.listens_for(MerchantRollingTransferConsumption, 'before_update')
@event.listens_for(MerchantRollingTransferConsumption, 'before_delete')
def protect_rolling_consumption(mapper, connection, target):
    raise ValueError('rolling transfer consumption rows are immutable')


class Requisite(Base, TimestampMixin):
    __tablename__='requisites'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trader_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    owner_name: Mapped[str]=mapped_column(String(160))
    full_name: Mapped[str|None]=mapped_column(String(160), nullable=True)
    method: Mapped[str]=mapped_column(String(32))
    value_encrypted: Mapped[str]=mapped_column(Text)
    bank_code: Mapped[str|None]=mapped_column(String(64), nullable=True, index=True)
    operator_code: Mapped[str|None]=mapped_column(String(64), nullable=True, index=True)
    bank_name: Mapped[str|None]=mapped_column(String(100), nullable=True)
    automation_id: Mapped[str|None]=mapped_column(String(120), nullable=True)
    last4: Mapped[str|None]=mapped_column(String(4), nullable=True)
    request_count: Mapped[int]=mapped_column(Integer, default=10)
    timeframe: Mapped[str]=mapped_column(String(16), default='час')
    success_delay_minutes: Mapped[int]=mapped_column(Integer, default=0)
    simultaneous_limit: Mapped[int]=mapped_column(Integer, default=1)
    min_check: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('100.00'))
    max_check: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('150000.00'))
    enabled: Mapped[bool]=mapped_column(Boolean, default=True)
    daily_limit: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('0.00'))
    operation_limit: Mapped[int]=mapped_column(Integer, default=0)
    status: Mapped[str]=mapped_column(String(32), default='active')
    usage_count: Mapped[int]=mapped_column(Integer, default=0)
    last_success_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    traffic_status: Mapped[str]=mapped_column(String(32), default='active', index=True)
    risk_score: Mapped[int]=mapped_column(Integer, default=0)
    failed_in_row: Mapped[int]=mapped_column(Integer, default=0)
    last_payment_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    auto_paused_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    limited_until: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    limited_max_active_payments: Mapped[int|None]=mapped_column(Integer, nullable=True)
    limited_max_amount: Mapped[Decimal|None]=mapped_column(Numeric(18,2), nullable=True)
    allow_high_amount: Mapped[bool]=mapped_column(Boolean, default=True)
    is_archived: Mapped[bool]=mapped_column(Boolean, default=False, index=True)
    archived_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)


class AntiscamGlobalSettings(Base, TimestampMixin):
    __tablename__='antiscam_global_settings'
    id: Mapped[int]=mapped_column(Integer, primary_key=True, default=1)
    antiscam_enabled: Mapped[bool]=mapped_column(Boolean, default=True)
    auto_disable_traffic_enabled: Mapped[bool]=mapped_column(Boolean, default=True)
    failed_payments_in_row_limit_requisite: Mapped[int]=mapped_column(Integer, default=5)
    failed_payments_in_row_limit_trader: Mapped[int]=mapped_column(Integer, default=10)
    min_payments_for_conversion_check: Mapped[int]=mapped_column(Integer, default=20)
    conversion_check_window_minutes: Mapped[int]=mapped_column(Integer, default=60)
    min_requisite_conversion_percent: Mapped[Decimal]=mapped_column(Numeric(5,2), default=Decimal('35.00'))
    min_trader_conversion_percent: Mapped[Decimal]=mapped_column(Numeric(5,2), default=Decimal('45.00'))
    conversion_drop_percent_limit: Mapped[Decimal]=mapped_column(Numeric(5,2), default=Decimal('40.00'))
    max_confirmation_delay_minutes: Mapped[int]=mapped_column(Integer, default=10)
    merchant_complaints_limit_requisite: Mapped[int]=mapped_column(Integer, default=3)
    merchant_complaints_limit_trader: Mapped[int]=mapped_column(Integer, default=5)
    high_amount_extra_risk_enabled: Mapped[bool]=mapped_column(Boolean, default=True)
    high_amount_threshold: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('100000.00'))
    freeze_withdrawals_on_trader_auto_pause: Mapped[bool]=mapped_column(Boolean, default=True)
    default_reinstate_mode: Mapped[str]=mapped_column(String(32), default='limited')
    limited_reinstate_duration_minutes: Mapped[int]=mapped_column(Integer, default=120)
    limited_reinstate_max_active_payments: Mapped[int]=mapped_column(Integer, default=3)
    limited_reinstate_max_amount: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('30000.00'))


class TraderAntiscamSettings(Base, TimestampMixin):
    __tablename__='trader_antiscam_settings'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trader_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'))
    use_global_antiscam_settings: Mapped[bool]=mapped_column(Boolean, default=True)
    antiscam_enabled: Mapped[bool]=mapped_column(Boolean, default=True)
    failed_payments_in_row_limit: Mapped[int]=mapped_column(Integer, default=10)
    min_conversion_percent: Mapped[Decimal]=mapped_column(Numeric(5,2), default=Decimal('45.00'))
    conversion_check_window_minutes: Mapped[int]=mapped_column(Integer, default=60)
    conversion_drop_percent_limit: Mapped[Decimal]=mapped_column(Numeric(5,2), default=Decimal('40.00'))
    max_confirmation_delay_minutes: Mapped[int]=mapped_column(Integer, default=10)
    max_active_payments_when_risky: Mapped[int]=mapped_column(Integer, default=3)
    allow_high_amount_traffic: Mapped[bool]=mapped_column(Boolean, default=False)
    risk_level: Mapped[str]=mapped_column(String(32), default='strict')
    auto_disable_requisites_enabled: Mapped[bool]=mapped_column(Boolean, default=True)
    auto_disable_trader_enabled: Mapped[bool]=mapped_column(Boolean, default=True)
    freeze_withdrawals_on_auto_pause: Mapped[bool]=mapped_column(Boolean, default=True)
    __table_args__=(
        UniqueConstraint(
            'trader_id',
            name='uq_trader_antiscam_settings_trader_id',
        ),
        Index('ix_trader_antiscam_settings_trader_id', 'trader_id'),
    )


class RequisiteAntiscamSettings(Base, TimestampMixin):
    __tablename__='requisite_antiscam_settings'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    requisite_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), ForeignKey('requisites.id', ondelete='CASCADE'))
    use_global_antiscam_settings: Mapped[bool]=mapped_column(Boolean, default=True)
    antiscam_enabled: Mapped[bool]=mapped_column(Boolean, default=True)
    failed_payments_in_row_limit: Mapped[int]=mapped_column(Integer, default=5)
    min_conversion_percent: Mapped[Decimal]=mapped_column(Numeric(5,2), default=Decimal('35.00'))
    conversion_check_window_minutes: Mapped[int]=mapped_column(Integer, default=60)
    max_confirmation_delay_minutes: Mapped[int]=mapped_column(Integer, default=10)
    allow_high_amount_traffic: Mapped[bool]=mapped_column(Boolean, default=True)
    limited_max_active_payments: Mapped[int]=mapped_column(Integer, default=3)
    limited_max_amount: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('30000.00'))
    __table_args__=(
        UniqueConstraint(
            'requisite_id',
            name='uq_requisite_antiscam_settings_requisite_id',
        ),
        Index(
            'ix_requisite_antiscam_settings_requisite_id',
            'requisite_id',
        ),
    )

class Appeal(Base, TimestampMixin):
    __tablename__='appeals'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    operation_type: Mapped[str]=mapped_column(String(32))
    operation_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), index=True)
    status: Mapped[str]=mapped_column(String(32), default=AppealStatus.opened.value)
    created_by: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('users.id'), nullable=True)
    decision: Mapped[str|None]=mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict]=mapped_column(JSON, default=dict)

class AppealMessage(Base, TimestampMixin):
    __tablename__='appeal_messages'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    appeal_id: Mapped[uuid.UUID]=mapped_column(ForeignKey('appeals.id', ondelete='CASCADE'), index=True)
    author_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('users.id'), nullable=True)
    message: Mapped[str]=mapped_column(Text)
    attachment_path: Mapped[str|None]=mapped_column(String(500), nullable=True)

class SmsMessage(Base, TimestampMixin):
    __tablename__='sms_messages'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider_message_id: Mapped[str]=mapped_column(String(128))
    sender: Mapped[str]=mapped_column(String(128))
    body: Mapped[str]=mapped_column(Text)
    parsed_amount: Mapped[Decimal|None]=mapped_column(Numeric(18,2), nullable=True)
    message_hash: Mapped[str|None]=mapped_column(String(64), unique=True, nullable=True)
    parse_confidence: Mapped[Decimal|None]=mapped_column(Numeric(5,2), nullable=True)
    review_required: Mapped[bool]=mapped_column(Boolean, default=False)
    linked_deposit_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('deposits.id'), nullable=True)
    processed: Mapped[bool]=mapped_column(Boolean, default=False)
    __table_args__=(
        UniqueConstraint(
            'provider_message_id',
            name='sms_messages_provider_message_id_key',
        ),
    )

class WebhookEvent(Base, TimestampMixin):
    __tablename__='webhook_events'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID]=mapped_column(ForeignKey('merchants.id', ondelete='CASCADE'), index=True)
    event_type: Mapped[str]=mapped_column(String(64))
    payload: Mapped[dict]=mapped_column(JSON)
    status: Mapped[str]=mapped_column(String(32), default='queued')
    attempts: Mapped[int]=mapped_column(Integer, default=0)
    max_attempts: Mapped[int]=mapped_column(Integer, default=5)
    last_error: Mapped[str|None]=mapped_column(Text, nullable=True)
    last_status_code: Mapped[int|None]=mapped_column(Integer, nullable=True)
    response_snippet: Mapped[str|None]=mapped_column(Text, nullable=True)
    next_retry_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True, index=True)
    lock_owner: Mapped[str|None]=mapped_column(String(64), nullable=True)
    locked_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    lease_until: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True, index=True)
    correlation_id: Mapped[str]=mapped_column(String(64), default=lambda: uuid.uuid4().hex)
    signing_key_id: Mapped[uuid.UUID|None]=mapped_column(
        UUID(as_uuid=True),
        ForeignKey('merchant_webhook_signing_keys.id', ondelete='SET NULL'),
        nullable=True,
        index=True,
    )
    __table_args__=(
        Index(
            'ix_webhook_events_delivery_due',
            'status',
            'next_attempt_at',
            'lease_until',
        ),
    )


class WebhookDeliveryAttempt(Base, TimestampMixin):
    __tablename__='webhook_delivery_attempts'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    webhook_event_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), ForeignKey('webhook_events.id', ondelete='CASCADE'), index=True)
    attempt_no: Mapped[int]=mapped_column(Integer, default=1)
    status: Mapped[str]=mapped_column(String(32), default='pending', index=True)
    status_code: Mapped[int|None]=mapped_column(Integer, nullable=True)
    error: Mapped[str|None]=mapped_column(Text, nullable=True)
    response_snippet: Mapped[str|None]=mapped_column(Text, nullable=True)
    __table_args__=(
        UniqueConstraint(
            'webhook_event_id',
            'attempt_no',
            name='uq_webhook_delivery_attempt_event_number',
        ),
    )


class MerchantWebhookSigningKey(Base, TimestampMixin):
    __tablename__='merchant_webhook_signing_keys'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID]=mapped_column(
        UUID(as_uuid=True),
        ForeignKey('merchants.id', ondelete='CASCADE'),
        index=True,
    )
    key_id: Mapped[str]=mapped_column(String(80), unique=True, index=True)
    encrypted_secret: Mapped[str]=mapped_column(String(255))
    status: Mapped[str]=mapped_column(String(16), default='active', index=True)
    retire_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True, index=True)
    revoked_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID|None]=mapped_column(
        UUID(as_uuid=True),
        ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True,
        index=True,
    )
    __table_args__=(
        CheckConstraint(
            "status IN ('active','retiring','revoked')",
            name='ck_merchant_webhook_signing_keys_status',
        ),
        Index(
            'uq_merchant_webhook_signing_keys_one_active',
            'merchant_id',
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )


class ApiReplayNonce(Base, TimestampMixin):
    __tablename__='api_replay_nonces'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    api_key_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), ForeignKey('api_keys.id', ondelete='CASCADE'), index=True)
    signature: Mapped[str]=mapped_column(String(128), index=True)
    request_hash: Mapped[str]=mapped_column(String(64), index=True)
    timestamp: Mapped[int]=mapped_column(Integer, index=True)
    expires_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), index=True)
    __table_args__=(UniqueConstraint('api_key_id','signature','request_hash', name='uq_api_replay_signature_hash'),)

class ApiRequestLog(Base, TimestampMixin):
    __tablename__='api_request_logs'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    method: Mapped[str]=mapped_column(String(16))
    path: Mapped[str]=mapped_column(String(500))
    ip: Mapped[str]=mapped_column(String(64))
    status_code: Mapped[int]=mapped_column(Integer, default=0)
    request_id: Mapped[str]=mapped_column(String(64), index=True)

class AggregatorAccount(Base, TimestampMixin):
    __tablename__='aggregator_accounts'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    platform_merchant_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), ForeignKey('merchants.id', ondelete='RESTRICT'), index=True)
    name: Mapped[str]=mapped_column(String(160))
    status: Mapped[str]=mapped_column(String(32), default='active', index=True)
    api_key: Mapped[str]=mapped_column(String(80))
    secret_hash: Mapped[str]=mapped_column(String(255))
    callback_url: Mapped[str|None]=mapped_column(String(500), nullable=True)
    success_url: Mapped[str|None]=mapped_column(String(500), nullable=True)
    fail_url: Mapped[str|None]=mapped_column(String(500), nullable=True)
    commission_percent: Mapped[Decimal]=mapped_column(Numeric(8,4), default=Decimal('0.00'))
    min_payment_amount: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('100.00'))
    max_payment_amount: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('150000.00'))
    daily_limit: Mapped[Decimal|None]=mapped_column(Numeric(18,2), nullable=True)
    monthly_limit: Mapped[Decimal|None]=mapped_column(Numeric(18,2), nullable=True)
    balance: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('0.00'))
    hold_balance: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('0.00'))
    total_turnover: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('0.00'))
    is_archived: Mapped[bool]=mapped_column(Boolean, default=False, index=True)
    archived_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__=(
        UniqueConstraint(
            'api_key',
            name='uq_aggregator_accounts_api_key',
        ),
        UniqueConstraint(
            'name',
            name='uq_aggregator_accounts_name',
        ),
        Index('ix_aggregator_accounts_api_key', 'api_key'),
        Index('ix_aggregator_accounts_name', 'name'),
    )


class AggregatorMerchant(Base, TimestampMixin):
    __tablename__='aggregator_merchants'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    aggregator_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), ForeignKey('aggregator_accounts.id', ondelete='CASCADE'), index=True)
    external_merchant_id: Mapped[str]=mapped_column(String(128), index=True)
    merchant_name: Mapped[str]=mapped_column(String(160))
    merchant_callback_url: Mapped[str|None]=mapped_column(String(500), nullable=True)
    merchant_secret_hash: Mapped[str]=mapped_column(String(255))
    status: Mapped[str]=mapped_column(String(32), default='active', index=True)
    commission_percent: Mapped[Decimal|None]=mapped_column(Numeric(8,4), nullable=True)
    is_archived: Mapped[bool]=mapped_column(Boolean, default=False, index=True)
    archived_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__=(UniqueConstraint('aggregator_id','external_merchant_id', name='uq_aggregator_merchants_external'),)


class AggregatorPayment(Base, TimestampMixin):
    __tablename__='aggregator_payments'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    aggregator_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), ForeignKey('aggregator_accounts.id', ondelete='CASCADE'), index=True)
    aggregator_merchant_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('aggregator_merchants.id', ondelete='SET NULL'), nullable=True, index=True)
    merchant_order_id: Mapped[str]=mapped_column(String(128), index=True)
    aggregator_order_id: Mapped[str]=mapped_column(String(128), index=True)
    platform_payment_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), ForeignKey('deposits.id', ondelete='RESTRICT'))
    amount: Mapped[Decimal]=mapped_column(Numeric(18,2))
    currency: Mapped[str]=mapped_column(String(8), default='RUB')
    payment_method: Mapped[str]=mapped_column(String(32))
    status: Mapped[str]=mapped_column(String(32), default='created', index=True)
    client_id: Mapped[str|None]=mapped_column(String(128), nullable=True)
    client_ip: Mapped[str|None]=mapped_column(String(64), nullable=True)
    callback_url_to_merchant: Mapped[str|None]=mapped_column(String(500), nullable=True)
    callback_status: Mapped[str]=mapped_column(String(32), default='pending', index=True)
    callback_attempts: Mapped[int]=mapped_column(Integer, default=0)
    last_callback_error: Mapped[str|None]=mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False)
    paid_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict]=mapped_column(JSON, default=dict)
    __table_args__=(
        UniqueConstraint(
            'platform_payment_id',
            name='uq_aggregator_payments_platform_payment_id',
        ),
        UniqueConstraint('aggregator_id','aggregator_order_id', name='uq_aggregator_payments_aggregator_order'),
        UniqueConstraint('aggregator_id','merchant_order_id', name='uq_aggregator_payments_merchant_order'),
        Index(
            'ix_aggregator_payments_platform_payment_id',
            'platform_payment_id',
        ),
    )


class AggregatorCallbackLog(Base, TimestampMixin):
    __tablename__='aggregator_callback_logs'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    direction: Mapped[str]=mapped_column(String(32), index=True)
    related_payment_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), ForeignKey('aggregator_payments.id', ondelete='CASCADE'), index=True)
    target_url: Mapped[str]=mapped_column(String(500))
    payload_json: Mapped[dict]=mapped_column(JSON, default=dict)
    response_status_code: Mapped[int|None]=mapped_column(Integer, nullable=True)
    response_body: Mapped[str|None]=mapped_column(Text, nullable=True)
    status: Mapped[str]=mapped_column(String(32), default='pending', index=True)
    attempt: Mapped[int]=mapped_column(Integer, default=0)
    error_message: Mapped[str|None]=mapped_column(Text, nullable=True)
    next_retry_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)


class AggregatorReplayNonce(Base, TimestampMixin):
    __tablename__='aggregator_replay_nonces'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    aggregator_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), ForeignKey('aggregator_accounts.id', ondelete='CASCADE'), index=True)
    signature: Mapped[str]=mapped_column(String(128), index=True)
    request_hash: Mapped[str]=mapped_column(String(64), index=True)
    timestamp: Mapped[int]=mapped_column(Integer, index=True)
    expires_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), index=True)
    __table_args__=(UniqueConstraint('aggregator_id','signature','request_hash', name='uq_aggregator_replay_signature_hash'),)

class AuditLog(Base, TimestampMixin):
    __tablename__='audit_logs'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    action: Mapped[str]=mapped_column(String(160), index=True)
    target_type: Mapped[str]=mapped_column(String(80))
    target_id: Mapped[str|None]=mapped_column(String(128), nullable=True)
    ip: Mapped[str|None]=mapped_column(String(64), nullable=True)
    details: Mapped[dict]=mapped_column(JSON, default=dict)

class FeeRule(Base, TimestampMixin):
    __tablename__='fee_rules'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    method: Mapped[str]=mapped_column(String(32))
    percent: Mapped[Decimal]=mapped_column(Numeric(8,4), default=Decimal('0'))
    fixed: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('0'))
    entity_type: Mapped[str]=mapped_column(String(32), default='merchant', index=True)
    entity_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    fee_side: Mapped[str]=mapped_column(String(32), default='merchant_fee', index=True)
    payment_method: Mapped[str]=mapped_column(String(32), default='sbp', index=True)
    currency: Mapped[str]=mapped_column(String(8), default='RUB', index=True)
    min_amount: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('0.00'))
    max_amount: Mapped[Decimal|None]=mapped_column(Numeric(18,2), nullable=True)
    rate_percent: Mapped[Decimal]=mapped_column(Numeric(8,4), default=Decimal('0.0000'))
    effective_from: Mapped[datetime]=mapped_column(DateTime(timezone=True))
    effective_to: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool]=mapped_column(Boolean, default=True, index=True)
    version: Mapped[int]=mapped_column(Integer, default=1)
    created_by: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), nullable=True)
    updated_by: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), nullable=True)
    __table_args__=(
        CheckConstraint(
            "entity_type IN ('merchant','trader','aggregator')",
            name='ck_fee_rules_entity_type',
        ),
        CheckConstraint(
            "entity_id <> '00000000-0000-0000-0000-000000000000'::uuid",
            name='ck_fee_rules_entity_id_nonzero',
        ),
        CheckConstraint(
            "(entity_type = 'merchant' AND fee_side = 'merchant_fee') OR "
            "(entity_type IN ('trader','aggregator') AND fee_side = 'executor_fee')",
            name='ck_fee_rules_entity_fee_side',
        ),
        CheckConstraint('min_amount >= 0', name='ck_fee_rules_min_amount_nonnegative'),
        CheckConstraint('max_amount IS NULL OR max_amount > min_amount', name='ck_fee_rules_amount_range'),
        CheckConstraint('rate_percent >= 0 AND rate_percent <= 100', name='ck_fee_rules_rate_percent'),
        CheckConstraint('effective_to IS NULL OR effective_to > effective_from', name='ck_fee_rules_effective_period'),
        Index(
            'ix_fee_rules_lookup',
            'entity_type', 'entity_id', 'fee_side', 'payment_method', 'currency',
            'is_active', 'effective_from',
        ),
        Index('ix_fee_rules_entity_scope', 'entity_type', 'entity_id'),
        Index(
            'uq_fee_rules_active_exact',
            'entity_type',
            'entity_id',
            'fee_side',
            'payment_method',
            'currency',
            'min_amount',
            'max_amount',
            'effective_from',
            'effective_to',
            unique=True,
            postgresql_where=text('is_active'),
            postgresql_nulls_not_distinct=True,
        ),
    )


class OperationFeeSnapshot(Base, TimestampMixin):
    __tablename__='operation_fee_snapshots'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    deposit_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), ForeignKey('deposits.id', ondelete='RESTRICT'))
    merchant_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), ForeignKey('merchants.id', ondelete='RESTRICT'), nullable=False, index=True)
    merchant_rate_rule_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), ForeignKey('fee_rules.id', ondelete='RESTRICT'), nullable=False)
    merchant_rate_version: Mapped[int]=mapped_column(Integer)
    merchant_rate_percent: Mapped[Decimal]=mapped_column(Numeric(8,4))
    merchant_fee_amount: Mapped[Decimal]=mapped_column(Numeric(18,2))
    executor_type: Mapped[str]=mapped_column(String(32), index=True)
    executor_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), index=True)
    executor_rate_rule_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), ForeignKey('fee_rules.id', ondelete='RESTRICT'), nullable=False)
    executor_rate_version: Mapped[int]=mapped_column(Integer)
    executor_rate_percent: Mapped[Decimal]=mapped_column(Numeric(8,4))
    executor_fee_amount: Mapped[Decimal]=mapped_column(Numeric(18,2))
    platform_margin_percent: Mapped[Decimal]=mapped_column(Numeric(8,4))
    platform_income_amount: Mapped[Decimal]=mapped_column(Numeric(18,2))
    calculation_base_amount: Mapped[Decimal]=mapped_column(Numeric(18,2))
    currency: Mapped[str]=mapped_column(String(8), index=True)
    payment_method: Mapped[str]=mapped_column(String(32), index=True)
    rate_snapshot_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), index=True)
    settlement_status: Mapped[str]=mapped_column(String(32), default='pending', index=True)
    settled_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    ledger_reference: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), nullable=True)
    __table_args__=(
        UniqueConstraint(
            'deposit_id',
            name='uq_operation_fee_snapshots_deposit_id',
        ),
        Index(
            'ix_operation_fee_snapshots_deposit_id',
            'deposit_id',
            unique=True,
        ),
        CheckConstraint('merchant_rate_percent >= executor_rate_percent', name='ck_operation_snapshot_nonnegative_margin'),
        CheckConstraint('merchant_fee_amount = executor_fee_amount + platform_income_amount', name='ck_operation_snapshot_fee_invariant'),
        CheckConstraint("executor_type IN ('trader','aggregator')", name='ck_operation_snapshot_executor_type'),
    )


@event.listens_for(OperationFeeSnapshot, 'before_update')
def protect_operation_fee_snapshot(mapper, connection, target):
    allowed = {'settlement_status', 'settled_at', 'ledger_reference', 'updated_at'}
    changed = {
        attribute.key
        for attribute in inspect(target).attrs
        if attribute.history.has_changes()
    }
    forbidden = changed.difference(allowed)
    if forbidden:
        raise ValueError(
            'operation fee snapshot is immutable: ' + ', '.join(sorted(forbidden))
        )

class LimitRule(Base, TimestampMixin):
    __tablename__='limit_rules'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    method: Mapped[str|None]=mapped_column(String(32), nullable=True)
    min_amount: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('0'))
    max_amount: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('1000000'))
    daily_amount: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('0'))

class Blacklist(Base, TimestampMixin):
    __tablename__='blacklist'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str]=mapped_column(String(32), index=True)
    value: Mapped[str]=mapped_column(String(255), index=True)
    reason: Mapped[str]=mapped_column(String(500), default='')
    is_active: Mapped[bool]=mapped_column(Boolean, default=True)
    __table_args__=(UniqueConstraint('kind','value', name='uq_blacklist_kind_value'),)

class RequisiteUsage(Base, TimestampMixin):
    __tablename__='requisite_usage'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    requisite_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), ForeignKey('requisites.id', ondelete='CASCADE'), index=True)
    operation_type: Mapped[str]=mapped_column(String(32))
    operation_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), index=True)
    amount: Mapped[Decimal]=mapped_column(Numeric(18,2))
    status: Mapped[str]=mapped_column(String(32), default='created')

class KycProfile(Base, TimestampMixin):
    __tablename__='kyc_profiles'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID]=mapped_column(ForeignKey('merchants.id', ondelete='CASCADE'))
    status: Mapped[str]=mapped_column(String(32), default='not_started', index=True)
    legal_name: Mapped[str|None]=mapped_column(String(255), nullable=True)
    tax_id_encrypted: Mapped[str|None]=mapped_column(Text, nullable=True)
    country: Mapped[str]=mapped_column(String(2), default='RU')
    risk_level: Mapped[str]=mapped_column(String(32), default='standard')
    reviewed_by: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('users.id'), nullable=True)
    reviewed_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__=(
        UniqueConstraint(
            'merchant_id',
            name='kyc_profiles_merchant_id_key',
        ),
        Index('ix_kyc_profiles_merchant_id', 'merchant_id'),
    )

class RiskEvent(Base, TimestampMixin):
    __tablename__='risk_events'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('merchants.id', ondelete='CASCADE'), nullable=True, index=True)
    operation_type: Mapped[str]=mapped_column(String(32), index=True)
    operation_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    score: Mapped[int]=mapped_column(Integer, default=0)
    decision: Mapped[str]=mapped_column(String(32), default='allow', index=True)
    reason: Mapped[str]=mapped_column(String(500), default='')
    details: Mapped[dict]=mapped_column(JSON, default=dict)
    source: Mapped[str]=mapped_column(String(32), default='risk', index=True)
    target_type: Mapped[str|None]=mapped_column(String(32), nullable=True, index=True)
    target_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    trader_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    requisite_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('requisites.id', ondelete='SET NULL'), nullable=True, index=True)
    severity: Mapped[str|None]=mapped_column(String(32), nullable=True, index=True)
    old_status: Mapped[str|None]=mapped_column(String(32), nullable=True)
    new_status: Mapped[str|None]=mapped_column(String(32), nullable=True)
    risk_score_before: Mapped[int|None]=mapped_column(Integer, nullable=True)
    risk_score_after: Mapped[int|None]=mapped_column(Integer, nullable=True)
    payments_count_in_window: Mapped[int|None]=mapped_column(Integer, nullable=True)
    successful_count_in_window: Mapped[int|None]=mapped_column(Integer, nullable=True)
    failed_count_in_window: Mapped[int|None]=mapped_column(Integer, nullable=True)
    conversion_before: Mapped[Decimal|None]=mapped_column(Numeric(5,2), nullable=True)
    conversion_after: Mapped[Decimal|None]=mapped_column(Numeric(5,2), nullable=True)
    window_minutes: Mapped[int|None]=mapped_column(Integer, nullable=True)
    amount_at_risk: Mapped[Decimal|None]=mapped_column(Numeric(18,2), nullable=True)
    auto_action_taken: Mapped[str|None]=mapped_column(String(64), nullable=True, index=True)
    resolved_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    resolution_status: Mapped[str|None]=mapped_column(String(32), nullable=True, index=True)
    resolution_comment: Mapped[str|None]=mapped_column(Text, nullable=True)


class RiskDecisionRecord(Base, TimestampMixin):
    __tablename__='risk_decisions'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    risk_event_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('risk_events.id', ondelete='SET NULL'), nullable=True, index=True)
    actor_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    target_type: Mapped[str]=mapped_column(String(32), index=True)
    target_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), index=True)
    trader_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    requisite_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('requisites.id', ondelete='SET NULL'), nullable=True, index=True)
    decision: Mapped[str]=mapped_column(String(64), index=True)
    decision_reason: Mapped[str]=mapped_column(Text)
    proofs_checked: Mapped[bool]=mapped_column(Boolean, default=False)
    proof_source: Mapped[str]=mapped_column(String(160), default='Telegram рабочая группа')
    proof_reference: Mapped[str|None]=mapped_column(String(500), nullable=True)
    reinstate_mode: Mapped[str|None]=mapped_column(String(32), nullable=True)
    old_status: Mapped[str|None]=mapped_column(String(32), nullable=True)
    new_status: Mapped[str|None]=mapped_column(String(32), nullable=True)
    risk_score_before: Mapped[int|None]=mapped_column(Integer, nullable=True)
    risk_score_after: Mapped[int|None]=mapped_column(Integer, nullable=True)
    limited_until: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    max_active_payments: Mapped[int|None]=mapped_column(Integer, nullable=True)
    max_payment_amount: Mapped[Decimal|None]=mapped_column(Numeric(18,2), nullable=True)
    allow_high_amount: Mapped[bool]=mapped_column(Boolean, default=False)

class ReportExport(Base, TimestampMixin):
    __tablename__='report_exports'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('users.id'), nullable=True, index=True)
    report_type: Mapped[str]=mapped_column(String(64), index=True)
    status: Mapped[str]=mapped_column(String(32), default='completed')
    file_path: Mapped[str|None]=mapped_column(String(500), nullable=True)
    filters: Mapped[dict]=mapped_column(JSON, default=dict)


class PlatformCryptoWallet(Base):
    __tablename__ = 'platform_crypto_wallets'
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    asset: Mapped[str] = mapped_column(String(16), default='USDT')
    network: Mapped[str] = mapped_column(String(16), default='TRC20')
    address: Mapped[str] = mapped_column(String(128))
    label: Mapped[str | None] = mapped_column(String(160), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('users.id', ondelete='RESTRICT'),
        nullable=False,
    )
    deactivated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text('now()'), nullable=False
    )
    deactivated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    change_reason: Mapped[str] = mapped_column(String(500))
    __table_args__ = (
        UniqueConstraint(
            'version',
            name='uq_platform_crypto_wallets_version',
        ),
        CheckConstraint(
            "asset = 'USDT'",
            name='ck_platform_crypto_wallet_asset',
        ),
        CheckConstraint(
            "network = 'TRC20'",
            name='ck_platform_crypto_wallet_network',
        ),
        CheckConstraint(
            'version > 0',
            name='ck_platform_crypto_wallet_version',
        ),
        CheckConstraint(
            '(is_active AND deactivated_at IS NULL '
            'AND deactivated_by IS NULL) OR '
            '(NOT is_active AND deactivated_at IS NOT NULL)',
            name='ck_platform_crypto_wallet_lifecycle',
        ),
        Index(
            'uq_platform_crypto_wallet_active',
            'asset',
            'network',
            unique=True,
            postgresql_where=text('is_active'),
        ),
        Index('ix_platform_crypto_wallets_created_at', 'created_at'),
    )


class AIIntegrationConfig(Base, TimestampMixin):
    __tablename__ = 'ai_integration_configs'
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    provider: Mapped[str] = mapped_column(
        String(64), default='veyra_ai_office'
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    environment: Mapped[str] = mapped_column(String(16), default='local')
    base_url: Mapped[str] = mapped_column(String(500), default='')
    health_path: Mapped[str] = mapped_column(
        String(255), default='/api/health'
    )
    api_version: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    auth_type: Mapped[str] = mapped_column(String(16), default='none')
    encrypted_api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    encrypted_bearer_token: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    encrypted_hmac_secret: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    timeout_seconds: Mapped[Decimal] = mapped_column(
        Numeric(8, 3), default=Decimal('5.000')
    )
    connect_timeout_seconds: Mapped[Decimal] = mapped_column(
        Numeric(8, 3), default=Decimal('2.000')
    )
    max_retries: Mapped[int] = mapped_column(Integer, default=0)
    verify_tls: Mapped[bool] = mapped_column(Boolean, default=True)
    selected_events: Mapped[list] = mapped_column(JSON, default=list)
    inbound_commands_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False
    )
    last_connection_test_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_connection_test_status: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    last_connection_test_latency_ms: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    last_error_message_redacted: Mapped[str | None] = mapped_column(
        String(500), nullable=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True,
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True,
    )
    __table_args__ = (
        UniqueConstraint(
            'provider',
            name='uq_ai_integration_configs_provider',
        ),
        CheckConstraint(
            "provider = 'veyra_ai_office'",
            name='ck_ai_integration_config_provider',
        ),
        CheckConstraint(
            "environment IN ('local','staging','production')",
            name='ck_ai_integration_config_environment',
        ),
        CheckConstraint(
            "auth_type IN ('none','bearer','api_key','hmac')",
            name='ck_ai_integration_config_auth_type',
        ),
        CheckConstraint(
            'timeout_seconds >= 1 AND timeout_seconds <= 60',
            name='ck_ai_integration_config_timeout',
        ),
        CheckConstraint(
            'connect_timeout_seconds >= 0.1 '
            'AND connect_timeout_seconds <= timeout_seconds',
            name='ck_ai_integration_config_connect_timeout',
        ),
        CheckConstraint(
            'max_retries >= 0 AND max_retries <= 5',
            name='ck_ai_integration_config_retries',
        ),
        CheckConstraint(
            'inbound_commands_enabled = false',
            name='ck_ai_integration_config_no_inbound_commands',
        ),
        CheckConstraint(
            "last_connection_test_status IS NULL OR "
            "last_connection_test_status IN ('success','failed')",
            name='ck_ai_integration_config_test_status',
        ),
        CheckConstraint(
            'last_connection_test_latency_ms IS NULL '
            'OR last_connection_test_latency_ms >= 0',
            name='ck_ai_integration_config_latency',
        ),
    )


def _encrypt_sensitive_attribute(target, value, oldvalue, initiator):
    del target, oldvalue, initiator
    return encrypt_secret(value)


for _sensitive_attribute in (
    User.twofa_secret,
    ApiKey.secret_hash,
    Requisite.value_encrypted,
    AggregatorAccount.secret_hash,
    AggregatorMerchant.merchant_secret_hash,
    KycProfile.tax_id_encrypted,
    AIIntegrationConfig.encrypted_api_key,
    AIIntegrationConfig.encrypted_bearer_token,
    AIIntegrationConfig.encrypted_hmac_secret,
):
    event.listen(_sensitive_attribute, 'set', _encrypt_sensitive_attribute, retval=True)
