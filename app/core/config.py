from decimal import Decimal
from functools import lru_cache
from urllib.parse import urlparse

from cryptography.fernet import Fernet
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')
    APP_NAME: str = 'Processing Platform'
    APP_VERSION: str = '2.0.0-rc2'
    BRAND_CONFIG_PATH: str = 'branding/default.yml'
    ENV: str = 'local'
    DEBUG: bool = False
    SECRET_KEY: str = Field(min_length=32)
    JWT_ACCESS_MINUTES: int = 30
    JWT_REFRESH_DAYS: int = 14
    DATABASE_URL: str
    SYNC_DATABASE_URL: str
    REDIS_URL: str = 'redis://redis:6379/0'
    CELERY_BROKER_URL: str = 'redis://redis:6379/1'
    CELERY_RESULT_BACKEND: str = 'redis://redis:6379/2'
    CORS_ORIGINS: str = 'http://localhost:8000'
    ENCRYPTION_KEY: str = Field(min_length=32)
    SUPERADMIN_EMAIL: str = 'superadmin@example.com'
    SUPERADMIN_PASSWORD: str = Field(min_length=12)
    SUPERADMIN_2FA_SECRET: str = Field(min_length=16)
    SEED_DEMO_DATA: bool = False
    ALLOW_SANDBOX_KEYS_IN_PRODUCTION: bool = False
    PRODUCTION_ACTIVATION_ENABLED: bool = False
    PLATFORM_WEBHOOK_RETRY_LIMIT: int = Field(default=5, ge=1, le=20)
    WEBHOOK_LEASE_SECONDS: int = Field(default=120, ge=15, le=3600)
    WEBHOOK_RETRY_BASE_SECONDS: int = Field(default=30, ge=1, le=3600)
    WEBHOOK_RETRY_MAX_SECONDS: int = Field(default=3600, ge=1, le=86400)
    WEBHOOK_RETRY_JITTER_SECONDS: int = Field(default=15, ge=0, le=600)
    WEBHOOK_RETRY_AFTER_MAX_SECONDS: int = Field(default=3600, ge=1, le=86400)
    WEBHOOK_SIGNING_KEY_OVERLAP_SECONDS: int = Field(
        default=86400,
        ge=60,
        le=2592000,
    )
    RATE_LIMIT_DEFAULT: str = '120/minute'
    LOGIN_RATE_LIMIT: str = '10/minute'
    AUTH_FAILURE_LIMIT: int = 7
    AUTH_LOCKOUT_SECONDS: int = 900
    TWOFA_FAILURE_LIMIT: int = 5
    TWOFA_LOCKOUT_SECONDS: int = 900
    REFRESH_RATE_LIMIT: str = '30/minute'
    MERCHANT_RATE_LIMIT: str = '300/minute'
    RATE_LIMIT_FAIL_CLOSED_IN_PRODUCTION: bool = True
    RATE_LIMIT_REDIS_FAILURE_RETRY_AFTER_SECONDS: int = 30
    HMAC_TIMESTAMP_TOLERANCE_SECONDS: int = 300
    MERCHANT_HMAC_V1_ENABLED: bool = False
    SESSION_COOKIE_NAME: str = 'processing_session'
    SESSION_COOKIE_MAX_AGE_SECONDS: int = 7200
    CSRF_TRUSTED_ORIGINS: str = 'http://localhost:8000,http://127.0.0.1:8000'
    MAX_REQUEST_BODY_BYTES: int = 2_000_000
    DEFAULT_MIN_AMOUNT: int = 1
    DEFAULT_MAX_AMOUNT: int = 1_000_000
    DOCS_ENABLED: bool = False
    OPENAPI_ENABLED: bool = False
    TRUSTED_HOSTS: str = 'localhost,127.0.0.1,0.0.0.0,api,host.docker.internal'
    TRUSTED_PROXY_IPS: str = '127.0.0.1,::1,172.16.0.0/12'
    HSTS_ENABLED: bool = False
    HSTS_MAX_AGE_SECONDS: int = 31_536_000
    LOG_LEVEL: str = 'INFO'
    LOG_FORMAT: str = 'json'
    REQUEST_ID_HEADER: str = 'X-Request-ID'
    METRICS_ENABLED: bool = True
    METRICS_TOKEN: str = ''
    SETTLEMENT_USDT_RUB_RATE: Decimal = Decimal('100.00')
    SETTLEMENT_TRANSFER_FEE_USDT: Decimal = Decimal('5.00')
    RAPIRA_RATES_ENABLED: bool = True
    RAPIRA_RATES_URL: str = 'https://api.rapira.net/open/market/rates'
    RAPIRA_RATES_TIMEOUT_SECONDS: float = 5.0
    RAPIRA_RATES_CACHE_SECONDS: int = 15
    DEPOSIT_PROCESSING_TTL_SECONDS: int = Field(default=900, ge=60, le=86400)
    ROLLING_RAPIRA_MAX_AGE_SECONDS: int = Field(default=60, ge=5, le=3600)
    SECRET_FLASH_TTL_SECONDS: int = 300
    CABINET_POLL_INTERVAL_SECONDS: int = 10

    @property
    def cors_list(self) -> list[str]:
        return [x.strip() for x in self.CORS_ORIGINS.split(',') if x.strip()]

    @property
    def csrf_origins(self) -> list[str]:
        return [x.strip().rstrip('/') for x in self.CSRF_TRUSTED_ORIGINS.split(',') if x.strip()]

    @property
    def trusted_hosts(self) -> list[str]:
        return [x.strip() for x in self.TRUSTED_HOSTS.split(',') if x.strip()]

    @property
    def trusted_proxy_ips(self) -> list[str]:
        return [x.strip() for x in self.TRUSTED_PROXY_IPS.split(',') if x.strip()]

    @property
    def is_production(self) -> bool:
        return self.ENV.lower() in {'production', 'prod'}

    @model_validator(mode='after')
    def validate_production_safety(self):
        if not self.is_production:
            return self

        insecure_values = {
            'SECRET_KEY': (self.SECRET_KEY, 48),
            'SUPERADMIN_PASSWORD': (self.SUPERADMIN_PASSWORD, 16),
            'SUPERADMIN_2FA_SECRET': (self.SUPERADMIN_2FA_SECRET, 16),
            'METRICS_TOKEN': (self.METRICS_TOKEN, 24),
        }
        for name, (value, min_length) in insecure_values.items():
            if _looks_unsafe_secret(value, min_length=min_length):
                raise ValueError(f'{name} must be set to a strong production value')

        if _looks_unsafe_secret(self.ENCRYPTION_KEY, min_length=32):
            raise ValueError('ENCRYPTION_KEY must be set to a strong production value')
        try:
            Fernet(self.ENCRYPTION_KEY.encode('utf-8'))
        except Exception as exc:
            raise ValueError('ENCRYPTION_KEY must be a valid Fernet key') from exc

        if self.DEBUG:
            raise ValueError('DEBUG must be false in production')
        if self.SEED_DEMO_DATA:
            raise ValueError('SEED_DEMO_DATA must be false in production')
        if self.ALLOW_SANDBOX_KEYS_IN_PRODUCTION:
            raise ValueError('ALLOW_SANDBOX_KEYS_IN_PRODUCTION must be false in production')
        if self.CABINET_POLL_INTERVAL_SECONDS < 5:
            raise ValueError('CABINET_POLL_INTERVAL_SECONDS must be at least 5 in production')
        if self.SUPERADMIN_EMAIL.strip().lower() == 'superadmin@example.com':
            raise ValueError('SUPERADMIN_EMAIL must not use the known local account in production')
        if not self.HSTS_ENABLED:
            raise ValueError('HSTS_ENABLED must be true in production')
        if '*' in self.trusted_hosts or '0.0.0.0' in self.trusted_hosts:
            raise ValueError('TRUSTED_HOSTS must not contain * or 0.0.0.0 in production')
        if '*' in self.trusted_proxy_ips or '0.0.0.0/0' in self.trusted_proxy_ips:
            raise ValueError('TRUSTED_PROXY_IPS must not contain * or 0.0.0.0/0 in production')
        if '*' in self.cors_list:
            raise ValueError('CORS_ORIGINS must not contain * in production when credentials are enabled')
        if any(origin.startswith('http://localhost') or origin.startswith('http://127.0.0.1') for origin in self.cors_list):
            raise ValueError('CORS_ORIGINS must not contain localhost in production')
        if any(origin.startswith('http://localhost') or origin.startswith('http://127.0.0.1') for origin in self.csrf_origins):
            raise ValueError('CSRF_TRUSTED_ORIGINS must not contain localhost in production')
        if self.DOCS_ENABLED:
            raise ValueError('DOCS_ENABLED must be false in production')
        if self.OPENAPI_ENABLED:
            raise ValueError('OPENAPI_ENABLED must be false in production')
        if 'CHANGE_ME_STRONG_RANDOM_VALUE' in self.DATABASE_URL or 'CHANGE_ME_STRONG_RANDOM_VALUE' in self.SYNC_DATABASE_URL:
            raise ValueError('DATABASE_URL must not use the demo database password in production')
        for name, value in {
            'DATABASE_URL': self.DATABASE_URL,
            'SYNC_DATABASE_URL': self.SYNC_DATABASE_URL,
        }.items():
            parsed = urlparse(value)
            if parsed.scheme.startswith('sqlite'):
                raise ValueError(f'{name} must not use sqlite in production')
            if parsed.hostname in {'localhost', '127.0.0.1', '::1'}:
                raise ValueError(f'{name} must not point to localhost in production')
            if _looks_unsafe_password(parsed.password, username=parsed.username):
                raise ValueError(f'{name} must use a strong non-default database password in production')
        for name, value in {
            'REDIS_URL': self.REDIS_URL,
            'CELERY_BROKER_URL': self.CELERY_BROKER_URL,
            'CELERY_RESULT_BACKEND': self.CELERY_RESULT_BACKEND,
        }.items():
            parsed = urlparse(value)
            if parsed.scheme.startswith('redis'):
                if parsed.hostname in {'localhost', '127.0.0.1', '::1'}:
                    raise ValueError(f'{name} must not point to localhost in production')
                if not parsed.password:
                    raise ValueError(f'{name} must include a password in production')
                if _looks_unsafe_password(parsed.password, username=parsed.username):
                    raise ValueError(f'{name} must use a strong non-default password in production')
        return self


UNSAFE_VALUES = {
    '',
    'admin',
    'casino',
    'changeme',
    'demo',
    'password',
    'postgres',
    'CHANGE_ME_STRONG_RANDOM_VALUE',
    'redis',
    'secret',
    'superadmin',
}


def _looks_unsafe_secret(value: str | None, *, min_length: int) -> bool:
    clean = (value or '').strip()
    lower = clean.lower()
    return (
        len(clean) < min_length
        or lower in UNSAFE_VALUES
        or lower.startswith('change_me')
        or lower.startswith('change-me')
    )


def _looks_unsafe_password(value: str | None, *, username: str | None = None) -> bool:
    clean = (value or '').strip()
    lower = clean.lower()
    return (
        len(clean) < 16
        or lower in UNSAFE_VALUES
        or lower.startswith('change_me')
        or lower.startswith('change-me')
        or bool(username and lower == username.strip().lower())
    )

@lru_cache
def get_settings():
    return Settings()
settings = get_settings()
