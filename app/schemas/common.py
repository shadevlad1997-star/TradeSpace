from decimal import Decimal
from datetime import datetime, timezone
from uuid import UUID
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator
from app.core.enums import AppealStatus, BlacklistKind, KycStatus, PaymentMethod, RiskDecision, Role
from app.core.mobile_operators import find_mobile_operator
from app.core.payment_methods import normalize_payment_method
from app.core.russian_banks import find_bank
from app.core.validators import validate_ip_whitelist, validate_public_webhook_url

class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = 'bearer'

class LoginIn(BaseModel):
    email: str = Field(min_length=5, max_length=255)
    password: str = Field(min_length=8, max_length=128)
    otp: str | None = Field(default=None, max_length=8)

    @field_validator('email')
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip().lower()
        if '@' not in value or value.startswith('@') or value.endswith('@'):
            raise ValueError('invalid email')
        return value

class RefreshIn(BaseModel):
    refresh_token: str = Field(min_length=40)

class MerchantCreate(BaseModel):
    email: str = Field(min_length=5, max_length=255)
    password: str = Field(min_length=12, max_length=128)
    name: str = Field(min_length=2, max_length=160)
    webhook_url: str | None = None
    ip_whitelist: list[str] = []
    sandbox_mode: bool = True

    @field_validator('email')
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip().lower()
        if '@' not in value or value.startswith('@') or value.endswith('@'):
            raise ValueError('invalid email')
        return value

    @field_validator('webhook_url')
    @classmethod
    def validate_webhook(cls, value: str | None) -> str | None:
        return validate_public_webhook_url(value)

    @field_validator('ip_whitelist')
    @classmethod
    def validate_ips(cls, value: list[str]) -> list[str]:
        return validate_ip_whitelist(value)

class AggregatorCreate(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    callback_url: str | None = None
    success_url: str | None = None
    fail_url: str | None = None
    commission_percent: Decimal = Field(default=0, ge=0, le=100, decimal_places=4)
    min_payment_amount: Decimal = Field(default=100, ge=0, decimal_places=2)
    max_payment_amount: Decimal = Field(default=150000, gt=0, decimal_places=2)
    daily_limit: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    monthly_limit: Decimal | None = Field(default=None, ge=0, decimal_places=2)

    @field_validator('callback_url', 'success_url', 'fail_url')
    @classmethod
    def validate_urls(cls, value: str | None) -> str | None:
        return validate_public_webhook_url(value)

    @field_validator('max_payment_amount')
    @classmethod
    def validate_aggregator_max_amount(cls, value: Decimal, info) -> Decimal:
        min_amount = info.data.get('min_payment_amount')
        if min_amount is not None and value < min_amount:
            raise ValueError('max_payment_amount cannot be lower than min_payment_amount')
        return value

class AggregatorUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=160)
    callback_url: str | None = None
    success_url: str | None = None
    fail_url: str | None = None
    commission_percent: Decimal | None = Field(default=None, ge=0, le=100, decimal_places=4)
    min_payment_amount: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    max_payment_amount: Decimal | None = Field(default=None, gt=0, decimal_places=2)
    daily_limit: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    monthly_limit: Decimal | None = Field(default=None, ge=0, decimal_places=2)

    @field_validator('callback_url', 'success_url', 'fail_url')
    @classmethod
    def validate_update_urls(cls, value: str | None) -> str | None:
        return validate_public_webhook_url(value)


class AggregatorStatusIn(BaseModel):
    status: str = Field(max_length=32)

    @field_validator('status')
    @classmethod
    def validate_aggregator_status(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {'active', 'blocked', 'archived'}:
            raise ValueError('status must be active, blocked, or archived')
        return value

class AggregatorMerchantIn(BaseModel):
    external_merchant_id: str = Field(min_length=1, max_length=128)
    merchant_name: str = Field(min_length=2, max_length=160)
    merchant_callback_url: str | None = None
    merchant_secret_key: str | None = Field(default=None, min_length=12, max_length=255)
    status: str = Field(default='active', max_length=32)
    commission_percent: Decimal | None = Field(default=None, ge=0, le=100, decimal_places=4)

    @field_validator('merchant_callback_url')
    @classmethod
    def validate_merchant_callback(cls, value: str | None) -> str | None:
        return validate_public_webhook_url(value)

    @field_validator('status')
    @classmethod
    def validate_aggregator_merchant_status(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {'active', 'blocked', 'archived'}:
            raise ValueError('status must be active, blocked, or archived')
        return value

class AggregatorPaymentCreate(BaseModel):
    aggregator_order_id: str = Field(min_length=1, max_length=128)
    merchant_order_id: str = Field(min_length=1, max_length=128)
    external_merchant_id: str = Field(min_length=1, max_length=128)
    amount: Decimal = Field(gt=0, decimal_places=2)
    currency: str = Field(default='RUB', max_length=8)
    payment_method: str = Field(max_length=32)
    client_id: str | None = Field(default=None, max_length=128)
    client_ip: str | None = Field(default=None, max_length=64)
    callback_url: str | None = None
    success_url: str | None = None
    fail_url: str | None = None

    @field_validator('currency')
    @classmethod
    def validate_aggregator_currency(cls, value: str) -> str:
        value = value.upper().strip()
        if value != 'RUB':
            raise ValueError('only RUB currency is supported')
        return value

    @field_validator('payment_method')
    @classmethod
    def normalize_aggregator_payment_method(cls, value: str) -> str:
        value = value.strip().lower().replace('-', '_')
        mapping = {
            'card': 'c2c',
            'card_number': 'c2c',
            'bank_transfer': 'c2c',
            'sbp': 'sbp',
            'c2c': 'c2c',
            'mobile': 'mobile_commerce',
            'mobile_commerce': 'mobile_commerce',
            'other': 'c2c',
        }
        if value not in mapping:
            raise ValueError('payment_method must be card, card_number, sbp, bank_transfer, c2c, mobile, mobile_commerce, or other')
        return mapping[value]

    @field_validator('callback_url', 'success_url', 'fail_url')
    @classmethod
    def validate_payment_urls(cls, value: str | None) -> str | None:
        return validate_public_webhook_url(value)

class PaymentCreate(BaseModel):
    external_id: str = Field(min_length=1, max_length=128)
    amount: Decimal = Field(gt=0, decimal_places=2)
    currency: str = Field(default='RUB', max_length=8)
    method: PaymentMethod
    metadata: dict = Field(default_factory=dict)

    model_config = {
        'json_schema_extra': {
            'examples': [
                {
                    'external_id': 'order-10001',
                    'amount': '1250.00',
                    'currency': 'RUB',
                    'method': 'mobile_commerce',
                    'metadata': {'customer_id': 'demo-customer-1'},
                }
            ]
        }
    }

    @field_validator('currency')
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        value = value.upper().strip()
        if value != 'RUB':
            raise ValueError('only RUB currency is supported for payment operations')
        return value

    @field_validator('method', mode='before')
    @classmethod
    def normalize_payment_method(cls, value: PaymentMethod | str) -> PaymentMethod:
        if isinstance(value, str):
            normalized = normalize_payment_method(value, canonical_mobile=True)
            if not normalized:
                raise ValueError('unsupported payment method')
            return PaymentMethod(normalized)
        if value == PaymentMethod.mobile:
            return PaymentMethod.mobile_commerce
        return value

class PayoutCreate(PaymentCreate):
    destination: str = Field(min_length=3, max_length=255)

class OperationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    external_id: str
    amount: Decimal
    currency: str
    method: str
    status: str

class BalanceOut(BaseModel):
    available: Decimal
    frozen: Decimal
    currency: str

class RequisiteIn(BaseModel):
    owner_name: str
    full_name: str | None = Field(default=None, max_length=160)
    method: PaymentMethod
    value: str = Field(min_length=3, max_length=255)
    bank_code: str | None = Field(default=None, max_length=64)
    operator_code: str | None = Field(default=None, max_length=64)
    bank_name: str | None = None
    trader_id: UUID | None = None
    automation_id: str | None = Field(default=None, max_length=120)
    last4: str | None = Field(default=None, max_length=4)
    min_check: Decimal = Field(default=100, ge=0, decimal_places=2)
    max_check: Decimal = Field(default=150000, gt=0, decimal_places=2)
    request_count: int = Field(default=10, ge=0)
    timeframe: str = Field(default='час', max_length=16)
    success_delay_minutes: int = Field(default=0, ge=0, le=1440)
    simultaneous_limit: int = Field(default=1, ge=1, le=100)
    daily_limit: Decimal = Field(default=0, ge=0)
    operation_limit: int = Field(default=0, ge=0)

    @field_validator('max_check')
    @classmethod
    def validate_requisite_max_check(cls, value: Decimal, info) -> Decimal:
        min_check = info.data.get('min_check')
        if min_check is not None and value < min_check:
            raise ValueError('max_check cannot be lower than min_check')
        return value

    @field_validator('method', mode='before')
    @classmethod
    def normalize_requisite_method(cls, value: PaymentMethod | str) -> PaymentMethod:
        if isinstance(value, str):
            normalized = normalize_payment_method(value, canonical_mobile=True)
            if not normalized:
                raise ValueError('unsupported payment method')
            return PaymentMethod(normalized)
        if value == PaymentMethod.mobile:
            return PaymentMethod.mobile_commerce
        return value

    @field_validator('bank_code', 'operator_code', 'bank_name')
    @classmethod
    def strip_provider_value(cls, value: str | None) -> str | None:
        clean = (value or '').strip()
        return clean or None

    @model_validator(mode='after')
    def validate_provider_by_method(self):
        if self.method in {PaymentMethod.sbp, PaymentMethod.c2c}:
            bank = find_bank(self.bank_code or self.bank_name)
            if not bank or not bank.enabled:
                raise ValueError('bank_code is required and must be selected from enabled bank directory')
            self.bank_code = bank.code
            self.operator_code = None
            self.bank_name = bank.display_name
            return self

        if self.method == PaymentMethod.mobile_commerce:
            operator = find_mobile_operator(self.operator_code or self.bank_name)
            if not operator or not operator.enabled:
                raise ValueError('operator_code is required and must be selected from enabled mobile operator directory')
            self.operator_code = operator.code
            self.bank_code = None
            self.bank_name = operator.display_name
            return self

        raise ValueError('unsupported payment method')

class AppealMessageIn(BaseModel):
    message: str = Field(min_length=3, max_length=5000)
    attachment_path: str | None = Field(default=None, max_length=500)

    @field_validator('attachment_path')
    @classmethod
    def safe_attachment_path(cls, value: str | None) -> str | None:
        if value and ('..' in value or value.startswith(('/', '\\'))):
            raise ValueError('unsafe attachment path')
        return value

class SmsIn(BaseModel):
    provider_message_id: str = Field(min_length=1, max_length=128)
    sender: str = Field(min_length=1, max_length=128)
    body: str = Field(min_length=1, max_length=2000)

class AppealIn(BaseModel):
    operation_type: str
    operation_id: UUID
    message: str = Field(min_length=3, max_length=5000)

class AppealResolveIn(BaseModel):
    status: AppealStatus
    decision: str = Field(min_length=3, max_length=64)
    message: str | None = Field(default=None, max_length=5000)

class WebhookRetryIn(BaseModel):
    webhook_event_id: UUID

class DateRangeQuery(BaseModel):
    date_from: datetime | None = None
    date_to: datetime | None = None
    merchant_id: UUID | None = None
    method: PaymentMethod | None = None

class RiskEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    operation_type: str
    operation_id: UUID | None
    score: int
    decision: RiskDecision | str
    reason: str
    details: dict

class UserCreateIn(BaseModel):
    email: str = Field(min_length=5, max_length=255)
    password: str = Field(min_length=12, max_length=128)
    role: Role
    merchant_name: str | None = Field(default=None, max_length=160)

    @field_validator('email')
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip().lower()
        if '@' not in value or value.startswith('@') or value.endswith('@'):
            raise ValueError('invalid email')
        return value

class FeeRuleIn(BaseModel):
    entity_type: str = Field(default='merchant', pattern='^(merchant|trader|aggregator)$')
    entity_id: UUID | None = None
    fee_side: str = Field(default='merchant_fee', pattern='^(merchant_fee|executor_fee)$')
    payment_method: PaymentMethod | None = None
    currency: str = Field(default='RUB', min_length=3, max_length=8)
    min_amount: Decimal = Field(default=0, ge=0, decimal_places=2)
    max_amount: Decimal | None = Field(default=None, gt=0, decimal_places=2)
    rate_percent: Decimal | None = Field(default=None, ge=0, le=100, decimal_places=4)
    effective_from: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    effective_to: datetime | None = None
    # Backward-compatible input names; all persistence uses the versioned fields above.
    merchant_id: UUID | None = None
    method: PaymentMethod | None = None
    percent: Decimal | None = Field(default=None, ge=0, le=100, decimal_places=4)
    fixed: Decimal = Field(default=0, ge=0, decimal_places=2)

    @model_validator(mode='after')
    def normalize_versioned_fee_rule(self):
        self.entity_id = self.entity_id or self.merchant_id
        self.payment_method = self.payment_method or self.method
        self.rate_percent = self.rate_percent if self.rate_percent is not None else self.percent
        if self.entity_id is None:
            raise ValueError('entity_id is required')
        if self.payment_method is None:
            raise ValueError('payment_method is required')
        if self.rate_percent is None:
            raise ValueError('rate_percent is required')
        if self.fixed != Decimal('0'):
            raise ValueError('fixed fee is not supported by tiered percentage rules')
        if self.max_amount is not None and self.max_amount <= self.min_amount:
            raise ValueError('max_amount must be greater than min_amount')
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError('effective_to must be greater than effective_from')
        return self

class LimitRuleIn(BaseModel):
    merchant_id: UUID | None = None
    method: PaymentMethod | None = None
    min_amount: Decimal = Field(default=0, ge=0, decimal_places=2)
    max_amount: Decimal = Field(default=1000000, gt=0, decimal_places=2)
    daily_amount: Decimal = Field(default=0, ge=0, decimal_places=2)

    @field_validator('max_amount')
    @classmethod
    def validate_max_amount(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError('max_amount must be positive')
        return value

class BlacklistIn(BaseModel):
    kind: BlacklistKind
    value: str = Field(min_length=1, max_length=255)
    reason: str = Field(default='', max_length=500)
    is_active: bool = True

    @field_validator('value')
    @classmethod
    def normalize_value(cls, value: str) -> str:
        return value.strip()

class KycReviewIn(BaseModel):
    status: KycStatus
    risk_level: str = Field(default='standard', max_length=32)
    legal_name: str | None = Field(default=None, max_length=255)
    tax_id: str | None = Field(default=None, min_length=3, max_length=64)
    country: str = Field(default='RU', min_length=2, max_length=2)
    comment: str | None = Field(default=None, max_length=1000)

    @field_validator('risk_level')
    @classmethod
    def validate_risk_level(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {'standard', 'high', 'prohibited'}:
            raise ValueError('risk_level must be standard, high, or prohibited')
        return value

    @field_validator('country')
    @classmethod
    def normalize_country(cls, value: str) -> str:
        value = value.strip().upper()
        if len(value) != 2 or not value.isalpha():
            raise ValueError('country must be ISO-3166 alpha-2 code')
        return value
