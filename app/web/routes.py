import base64
import io
import json
import logging
import secrets
import uuid
from uuid import UUID
from pathlib import Path
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote_plus
from markupsafe import Markup, escape

import pyotp
import qrcode
from fastapi import APIRouter, Request, Depends, Form, HTTPException, File, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from redis.asyncio import Redis

from app.db.session import get_db
from app.models import User, Merchant, Balance, ApiKey, Deposit, Payout, Requisite, Appeal, AppealMessage, RefreshToken, AuditLog, MerchantSettlement, WebhookEvent, WebhookDeliveryAttempt, MerchantWebhookSigningKey, AggregatorAccount, AggregatorPayment, AggregatorCallbackLog, FeeRule, MerchantRollingAccount, MerchantRollingAllocation, MerchantRollingLedgerEntry, MerchantRollingTransfer, MerchantRollingTransferConsumption, TeamLeadAccrual, TeamLeadBalance, TeamLeadLedgerEntry, TeamLeadMerchantAccrual, TeamLeadMerchantAssignment, TeamLeadSettlement, TeamLeadTraderAssignment, AIIntegrationConfig
from app.core.rate_limit import hit_rate_limit
from app.core.security import auth_state_marker, decrypt_secret, verify_password, hash_password, encrypt_secret, reveal_text
from app.core.session import (
    PREVIEW_ROLE_SESSION_KEY,
    PREVIEW_SUBJECT_SESSION_KEY,
    SESSION_GENERATION_KEY,
    SESSION_REALM_KEY,
    activate_realm_session,
    issue_preview_context_token,
    realm_cabinet_path,
    realm_for_role,
    realm_login_path,
    request_auth_realm,
    session_view_fingerprint,
    session_view_matches,
    verify_preview_context_token,
)
from app.core.auth_hardening import is_auth_temporarily_locked, record_auth_failure, record_auth_success
from app.core.enums import AppealStatus, DepositStatus, PaymentMethod, Role, TrafficStatus
from app.core.access import can_view_appeal
from app.core.config import settings
from app.core.branding import branding_context, get_branding
from app.core.client_ip import client_ip
from app.core.validators import validate_ip_whitelist, validate_public_webhook_url
from app.core.mobile_operators import get_enabled_mobile_operators
from app.core.payment_methods import PAYMENT_METHOD_OPTIONS, PAYMENT_METHOD_REQUISITE_PLACEHOLDERS, normalize_payment_method, payment_method_label
from app.core.requisite_providers import get_requisite_provider_display, get_requisite_provider_label, get_requisite_provider_type, requisite_provider_display, requisite_provider_label, resolve_requisite_provider
from app.core.russian_banks import RF_BANKS, get_bank_display_name, get_enabled_banks, normalize_bank_name
from app.api.deps import STAFF_2FA_ROLES
from app.services.audit import audit
from app.services.appeals import APPEAL_REVIEW_TTL, TRADER_REJECTION_REASONS, appeal_deadline, appeal_remaining_seconds, approve_deposit_appeal, approve_expired_appeals, default_deadline, metadata as appeal_metadata, reject_deposit_appeal, set_metadata as set_appeal_metadata
from app.services.antiscam import (
    antiscam_dashboard_rows,
    check_payment_result_for_risk,
    get_global_settings,
    get_or_create_requisite_settings,
    get_or_create_trader_settings,
    reinstate_requisite,
    reinstate_trader,
)
from app.services.aggregator_enqueue import enqueue_aggregator_callback_delivery
from app.services.aggregators import create_aggregator_account, money, percent, regenerate_aggregator_secret, sync_payment_from_deposit
from app.services.ledger import LedgerError, manual_trader_adjustment
from app.services.requisites import deposit_trader_profit_amount, deposit_trader_settlement_amount, release_trader_deposit_hold
from app.services.deposit_confirmation import DepositConfirmationError, DepositConfirmationSystemError, confirm_deposit_payment, enqueue_deposit_confirmation_deliveries
from app.services.deposit_lifecycle import (
    expire_due_deposits,
    finalize_unsuccessful_deposit,
)
from app.services.deposit_ttl import deposit_deadline
from app.services.rapira import RollingRateUnavailable, get_rapira_rub_usdt_quote, get_strict_rolling_ask_quote
from app.services.settlements import (
    MerchantSettlementError,
    MerchantSettlementRateUnavailable,
    complete_merchant_settlement,
    create_merchant_settlement,
    merchant_settlement_quote,
    reject_merchant_settlement as reject_merchant_settlement_service,
)
from app.services.webhook_enqueue import enqueue_webhook_delivery
from app.services.webhooks import (
    queue_webhook,
    request_manual_webhook_retry,
)
from app.services.webhook_payloads import build_deposit_webhook_payload
from app.services.fee_tiers import (
    COMMISSION_TIER_METHODS,
    COMMISSION_TIER_RANGES,
    FeeConfigurationError,
    build_commission_tier_context,
    create_fee_rule as create_tier_fee_rule,
    replace_commission_tiers,
    replace_fee_rule as replace_tier_fee_rule,
)
from app.services.platform_income import platform_income_dashboard
from app.services.rolling import (
    RollingError,
    cancel_rolling_transfer,
    confirm_rolling_transfer,
    dispute_rolling_transfer,
    get_rolling_account,
    list_rolling_transfers,
    reconcile_rolling_account,
    register_rolling_transfer,
    rolling_overview,
    set_rolling_suspension,
)
from app.services.teamlead import (
    TEAMLEAD_SETTLEMENT_COOLDOWN,
    TeamLeadError,
    adjust_teamlead_balance,
    close_merchant_assignment,
    complete_teamlead_settlement,
    create_or_replace_assignment,
    create_or_replace_merchant_assignment,
    create_teamlead_settlement,
    get_teamlead_balance,
    reconcile_teamlead_account,
    reject_teamlead_settlement,
    reverse_teamlead_accruals_for_deposit,
)
from app.services.platform_wallet import (
    PlatformWalletError,
    get_active_platform_wallet,
    list_platform_wallet_history,
    platform_wallet_qr_png,
    set_active_platform_wallet,
)
from app.services.ai_office import (
    AIIntegrationError,
    AI_OFFICE_EVENT_OPTIONS,
    ai_config_view,
    ai_connection_snapshot,
    clear_ai_integration_secret,
    get_ai_integration_config,
    record_ai_connection_result,
    run_ai_connection_test_without_transaction,
    save_ai_integration_config,
)
from app.services.merchant_api_keys import (
    MerchantApiKeyError,
    api_key_fingerprint,
    api_key_mode_label,
    can_manage_merchant_api_keys,
    issue_merchant_api_key,
    normalize_api_key_mode,
    revoke_merchant_api_key,
    rotate_merchant_api_key,
)
from app.services.webhook_signing_keys import (
    WebhookSigningKeyError,
    issue_webhook_signing_key,
    rotate_webhook_signing_key,
    webhook_key_fingerprint,
)
from app.web.ui_messages import cabinet_redirect_url, resolve_ui_message
from app.web.ui_queries import (
    DEPOSIT_ACTIVE_STATUSES,
    DEPOSIT_TERMINAL_STATUSES,
    apply_trader_deposit_filters,
    normalized_deposit_filters,
    trader_deposit_aggregates,
    trader_deposit_scope,
)
from app.web.view_models import (
    build_attempts_by_event_id,
    build_audit_view,
    build_webhook_event_view,
    deposit_status_label,
    failure_reason_label,
    money_rub,
    short_identifier,
    traffic_status_class,
    traffic_status_label,
    webhook_status_class,
    webhook_status_label,
)

router = APIRouter(tags=['web'])
templates = Jinja2Templates(directory='app/templates')
security_logger = logging.getLogger('app.security')
_secret_flash_redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
AGGREGATOR_SECRET_FLASH_SESSION_KEY = 'aggregator_secret_flash_id'
AGGREGATOR_SECRET_FLASH_PREFIX = 'secret_flash:aggregator'
AGGREGATOR_SECRET_FLASH_UNAVAILABLE_MESSAGE = (
    'Aggregator secret rotated, but one-time display storage is unavailable. '
    'Generate a new secret after Redis is available.'
)


def _aggregator_secret_flash_key(user_id, flash_id: str) -> str:
    return f'{AGGREGATOR_SECRET_FLASH_PREFIX}:{user_id}:{flash_id}'


async def _redis_getdel(client, key: str):
    getdel = getattr(client, 'getdel', None)
    if getdel:
        return await getdel(key)
    value = await client.get(key)
    if value is not None:
        await client.delete(key)
    return value


async def _store_aggregator_secret_flash(request: Request, user: User, payload: dict, redis_client=None) -> bool:
    request.session.pop('aggregator_secret_flash', None)
    request.session.pop(AGGREGATOR_SECRET_FLASH_SESSION_KEY, None)
    flash_id = secrets.token_urlsafe(32)
    safe_payload = {
        'name': str(payload.get('name') or ''),
        'api_key': str(payload.get('api_key') or ''),
        'secret_key': str(payload.get('secret_key') or ''),
        'mode': str(payload.get('mode') or 'sandbox'),
    }
    try:
        client = redis_client or _secret_flash_redis
        await client.set(
            _aggregator_secret_flash_key(user.id, flash_id),
            json.dumps(safe_payload, ensure_ascii=False),
            ex=settings.SECRET_FLASH_TTL_SECONDS,
        )
    except Exception as exc:
        security_logger.warning(
            'aggregator_secret_flash_storage_unavailable',
            extra={'actor_id': str(user.id), 'reason': str(exc)[:300]},
        )
        return False
    request.session[AGGREGATOR_SECRET_FLASH_SESSION_KEY] = flash_id
    return True


async def _consume_aggregator_secret_flash(request: Request, user: User, redis_client=None) -> dict | None:
    request.session.pop('aggregator_secret_flash', None)
    flash_id = request.session.pop(AGGREGATOR_SECRET_FLASH_SESSION_KEY, None)
    if not flash_id:
        return None
    try:
        client = redis_client or _secret_flash_redis
        raw_payload = await _redis_getdel(client, _aggregator_secret_flash_key(user.id, flash_id))
    except Exception as exc:
        security_logger.warning(
            'aggregator_secret_flash_consume_unavailable',
            extra={'actor_id': str(user.id), 'reason': str(exc)[:300]},
        )
        return None
    if not raw_payload:
        return None
    try:
        payload = json.loads(raw_payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return {
        'name': str(payload.get('name') or ''),
        'api_key': str(payload.get('api_key') or ''),
        'secret_key': str(payload.get('secret_key') or ''),
        'mode': str(payload.get('mode') or 'sandbox'),
    }


def csrf_input(request: Request) -> Markup:
    token = request.scope.get('csrf_token', '')
    session_view = session_view_fingerprint(request.session)
    view_input = ''
    if session_view:
        view_input = f'<input type="hidden" name="session_view" value="{escape(session_view)}">'
    return Markup(f'<input type="hidden" name="csrf_token" value="{escape(token)}">{view_input}')


def csp_nonce(request: Request) -> str:
    return str(request.scope.get('csp_nonce', ''))


def _dt_for_display(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.replace('Z', '+00:00'))
        except ValueError:
            return None
    return None


def dt_compact(value) -> str:
    dt = _dt_for_display(value)
    if not dt:
        return '—'
    return dt.strftime('%d.%m.%Y %H:%M')


def dt_full(value) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or '')


templates.env.globals['csrf_input'] = csrf_input
templates.env.globals['csp_nonce'] = csp_nonce
templates.env.globals['brand'] = get_branding()
templates.env.globals['brand_css_variables'] = get_branding().css_variables()
templates.env.filters['payment_method_label'] = payment_method_label
templates.env.filters['bank_display'] = get_bank_display_name
templates.env.filters['provider_label'] = requisite_provider_label
templates.env.filters['dt_compact'] = dt_compact
templates.env.filters['dt_full'] = dt_full
templates.env.filters['money_rub'] = money_rub
templates.env.filters['short_identifier'] = short_identifier
templates.env.filters['deposit_status_label'] = deposit_status_label
templates.env.filters['failure_reason_label'] = failure_reason_label
templates.env.filters['traffic_status_label'] = traffic_status_label
templates.env.filters['traffic_status_class'] = traffic_status_class
templates.env.filters['webhook_status_label'] = webhook_status_label
templates.env.filters['webhook_status_class'] = webhook_status_class
templates.env.globals['provider_obj_type'] = get_requisite_provider_type
templates.env.globals['provider_obj_label'] = get_requisite_provider_label
templates.env.globals['provider_obj_display'] = get_requisite_provider_display

ROLE_TITLES = {
    'superadmin': 'Superadmin кабинет',
    'admin': 'Admin кабинет',
    'support': 'Support кабинет',
    'teamlead': 'TeamLead кабинет',
    'merchant': 'Merchant кабинет казино',
    'aggregator': 'Aggregator кабинет',
    'operator': 'Трейдер кабинет',
    'trader': 'Трейдер кабинет',
}
ROLE_DESCRIPTIONS = {
    'superadmin': 'Главный кабинет: создание Admin/Support/Merchant/Trader, полный контроль площадки, платежи, реквизиты, аудит, риски.',
    'admin': 'Операционное управление: создание Support/Merchant/Trader, заявки, мерчанты, трейдеры, лимиты, статистика.',
    'support': 'Только просмотр и поддержка: заявки пополнений/выплат, статусы, апелляции, SMS. Без доступа к деньгам и созданию кабинетов.',
    'teamlead': 'Собственный кабинет TeamLead: назначенные трейдеры, начисления от gross, баланс и вывод USDT TRC20.',
    'merchant': 'Кабинет казино: API-интеграция, баланс, пополнения/выплаты, webhook, статистика.',
    'aggregator': 'Кабинет агрегатора с изолированной авторизацией.',
    'operator': 'Кабинет трейдера: подключение реквизитов, обработка платежей и работа с SMS-подтверждениями.',
    'trader': 'Кабинет трейдера: подключение реквизитов и обработка платежей.',
}
ROLE_SECTIONS = {
    'superadmin': ['Обзор', 'Создать кабинет', 'Пользователи', 'TeamLead', 'Площадки/агрегаторы', 'Aggregators', 'Мерчанты', 'Rolling', 'Трейдеры', 'Пополнения', 'Выплаты', 'Кошелёк', 'Статистика', 'Реквизиты', 'Апелляции', 'Смс хаб', 'Интеграция с AI-офисом', 'Безопасность', 'Audit log', 'Risk / AML'],
    'admin': ['Обзор', 'Создать кабинет', 'TeamLead', 'Площадки/агрегаторы', 'Aggregators', 'Мерчанты', 'Трейдеры', 'Пополнения', 'Выплаты', 'Кошелёк', 'Статистика', 'Реквизиты', 'Апелляции', 'Смс хаб', 'Безопасность', 'Risk / AML'],
    'support': ['Обзор', 'Площадки/агрегаторы', 'Aggregators', 'Трейдеры', 'Все пополнения', 'Все выплаты', 'Апелляции', 'Смс хаб', 'История обращений', 'Безопасность'],
    'teamlead': ['TeamLead', 'Безопасность'],
    'merchant': ['Обзор', 'Пополнение', 'Выплаты', 'Кошелек', 'Статистика', 'API ключи', 'Webhook', 'Интеграция казино', 'Апелляции', 'Безопасность'],
    'aggregator': ['Обзор', 'Безопасность'],
    'operator': ['Мои реквизиты', 'Пополнения', 'Статистика', 'Выплаты', 'SMS подтверждения', 'Баланс', 'Апелляции', 'Безопасность'],
    'trader': ['Мои реквизиты', 'Пополнения', 'Статистика', 'Выплаты', 'SMS подтверждения', 'Баланс', 'Апелляции', 'Безопасность'],
}
if 'Антискам / Risk Control' not in ROLE_SECTIONS['superadmin']:
    ROLE_SECTIONS['superadmin'].insert(-3, 'Антискам / Risk Control')


# Hidden until AML module is implemented. Keep the section definition in
# ROLE_SECTIONS so it can be restored later without touching business logic.
HIDDEN_CABINET_SECTIONS = {'Risk / AML'}

for _income_role in ('superadmin', 'admin'):
    if 'Доход площадки' not in ROLE_SECTIONS[_income_role]:
        ROLE_SECTIONS[_income_role].insert(-1, 'Доход площадки')


def safe_decimal(value: str, default: str = '0') -> Decimal:
    try:
        return Decimal(str(value).replace(',', '.')).quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError):
        return Decimal(default)


def totp_qr_data_uri(email: str, secret: str) -> str:
    uri = pyotp.TOTP(secret).provisioning_uri(
        name=f'{email} @ processing-platform',
        issuer_name=get_branding().product_name,
    )
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()


def get_session_user_id(request: Request):
    return request.session.get('user_id')


def staff_2fa_setup_required(user: User) -> bool:
    return user.role in STAFF_2FA_ROLES and not user.twofa_enabled


def _session_marker(user: User) -> str:
    return auth_state_marker(user.password_hash)


SESSION_IDENTITY_CHANGED_CODE = 'session_identity_changed'
SESSION_IDENTITY_CHANGED_MESSAGE = 'В этом кабинете выполнен вход под другой учётной записью.'


def _mark_session_event(request: Request, reason: str) -> None:
    request.scope['session_event_reason'] = reason


async def _request_session_view(request: Request) -> str:
    headers = getattr(request, 'headers', {}) or {}
    query_params = getattr(request, 'query_params', {}) or {}
    candidate = (headers.get('x-session-view') or query_params.get('session_view') or '').strip()
    method = str(getattr(request, 'method', 'GET') or 'GET').upper()
    if candidate or method in {'GET', 'HEAD', 'OPTIONS'}:
        return candidate
    content_type = (headers.get('content-type') or '').lower()
    if 'application/x-www-form-urlencoded' not in content_type and 'multipart/form-data' not in content_type:
        return ''
    try:
        form = await request.form()
    except Exception:
        return ''
    return str(form.get('session_view') or '').strip()


async def _session_identity_changed(request: Request) -> bool:
    candidate = await _request_session_view(request)
    return bool(
        candidate
        and request.session.get('user_id')
        and not session_view_matches(request.session, candidate)
    )


def _session_changed_page(request: Request):
    _mark_session_event(request, SESSION_IDENTITY_CHANGED_CODE)
    realm = request_auth_realm(request)
    response = templates.TemplateResponse(
        request=request,
        name='session_changed.html',
        context={
            'request': request,
            **branding_context(),
            'message': SESSION_IDENTITY_CHANGED_MESSAGE,
            'login_path': realm_login_path(realm) if realm else '/login',
        },
        status_code=409,
    )
    response.headers['Cache-Control'] = 'no-store, max-age=0'
    return response


def _clear_preview_context(request: Request) -> None:
    request.session.pop(PREVIEW_ROLE_SESSION_KEY, None)
    request.session.pop(PREVIEW_SUBJECT_SESSION_KEY, None)


async def _trusted_trader_preview_subject(
    request: Request,
    viewer: User,
    db: AsyncSession,
    *,
    requested_role: str | None,
    accept_entry_token: bool,
) -> User | None:
    if viewer.role in TRADER_ROLES:
        return viewer if requested_role in {None, viewer.role, Role.trader.value} else None
    if viewer.role != Role.superadmin.value:
        return None
    preview_role = requested_role or str(
        request.session.get(PREVIEW_ROLE_SESSION_KEY) or ""
    )
    if preview_role != Role.trader.value:
        return None

    subject_id = None
    entry_token = request.query_params.get("preview_context", "").strip()
    if accept_entry_token and entry_token:
        subject_id = verify_preview_context_token(
            request.session,
            entry_token,
            preview_role=Role.trader.value,
            max_age=settings.SESSION_COOKIE_MAX_AGE_SECONDS,
        )
        if not subject_id:
            _clear_preview_context(request)
            return None
    elif (
        request.session.get(PREVIEW_ROLE_SESSION_KEY) == Role.trader.value
        and request.session.get(PREVIEW_SUBJECT_SESSION_KEY)
    ):
        subject_id = str(request.session[PREVIEW_SUBJECT_SESSION_KEY])
    if not subject_id:
        return None
    try:
        subject_uuid = UUID(subject_id)
    except ValueError:
        _clear_preview_context(request)
        return None
    subject = (await db.execute(
        select(User).where(
            User.id == subject_uuid,
            User.role.in_(list(TRADER_ROLES)),
            User.is_archived.is_(False),
        )
    )).scalar_one_or_none()
    if not subject:
        _clear_preview_context(request)
        return None
    request.session[PREVIEW_ROLE_SESSION_KEY] = Role.trader.value
    request.session[PREVIEW_SUBJECT_SESSION_KEY] = str(subject.id)
    return subject


def _preview_forbidden_page(request: Request, viewer: User, requested_role: str):
    realm = request_auth_realm(request) or realm_for_role(viewer.role)
    return templates.TemplateResponse(
        request=request,
        name='forbidden.html',
        context={
            'request': request,
            'user': viewer,
            'requested_role': requested_role,
            'cabinet_base': realm_cabinet_path(realm) if realm else '/cabinet',
            **branding_context(),
        },
        status_code=403,
    )


async def revoke_refresh_tokens(db: AsyncSession, user_id) -> None:
    rows = (await db.execute(select(RefreshToken).where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)))).scalars().all()
    now = datetime.now(timezone.utc)
    for row in rows:
        row.revoked_at = now


def _twofa_redirect() -> RedirectResponse:
    return _redirect('twofa_required', 'security')


MANAGED_USER_ROLES = {Role.admin.value, Role.support.value, Role.teamlead.value, Role.merchant.value, Role.operator.value, Role.trader.value}
TRADER_ROLES = {Role.operator.value, Role.trader.value}

DEPOSIT_ACTIVE_FOR_TRADER = {
    DepositStatus.created.value,
    DepositStatus.pending.value,
    DepositStatus.appeal_opened.value,
}


def _deposit_deadline(dep: Deposit) -> datetime:
    return deposit_deadline(dep)


def _deposit_webhook_payload(dep: Deposit) -> dict:
    return build_deposit_webhook_payload(dep)


async def expire_stale_deposits(db: AsyncSession) -> list:
    return await expire_due_deposits(db)


def _can_manage_requisite(user: User, req: Requisite) -> bool:
    if user.role in [Role.superadmin.value, Role.admin.value]:
        return True
    return user.role in [Role.operator.value, Role.trader.value] and str(req.trader_id) == str(user.id)


def _can_view_requisite_value(user: User, req: Requisite, *, merchant_related: bool = False) -> bool:
    if user.role in [Role.superadmin.value, Role.admin.value, Role.support.value]:
        return True
    if user.role in [Role.operator.value, Role.trader.value]:
        return str(req.trader_id) == str(user.id)
    if user.role == Role.merchant.value:
        return merchant_related
    return False



def display_requisite_value(req: Requisite) -> str:
    value = reveal_text(req.value_encrypted)
    return value or 'RE-SAVE REQUIRED'


def text_contains(value: object, query: str) -> bool:
    return query.lower() in str(value or '').lower()


def date_matches(value: object, date_filter: str) -> bool:
    if not date_filter:
        return True
    if isinstance(value, datetime):
        return value.date().isoformat() == date_filter
    return str(value or '').startswith(date_filter)


def amount_matches(value: object, amount_filter: str) -> bool:
    if not amount_filter:
        return True
    return safe_decimal(str(value or '0'), '0') == safe_decimal(amount_filter, '-1')


def deposit_matches_filters(dep: Deposit, req: Requisite | None, filters: dict[str, str]) -> bool:
    query = filters.get('query', '')
    if query and not (text_contains(dep.id, query) or text_contains(dep.external_id, query)):
        return False
    if not amount_matches(dep.amount, filters.get('amount', '')):
        return False
    if not date_matches(dep.created_at, filters.get('date', '')):
        return False
    requisite_query = filters.get('requisite', '')
    if requisite_query:
        requisite_text = ''
        if req:
            requisite_text = ' '.join([
                display_requisite_value(req),
                req.bank_name or '',
                req.full_name or '',
                req.owner_name or '',
            ])
        if not text_contains(requisite_text, requisite_query):
            return False
    return True


def appeal_matches_filters(appeal: Appeal, dep: Deposit | None, detail: dict, filters: dict[str, str]) -> bool:
    query = filters.get('query', '')
    if query and not (
        text_contains(appeal.id, query)
        or text_contains(appeal.operation_id, query)
        or text_contains(dep.external_id if dep else '', query)
    ):
        return False
    if not amount_matches(detail.get('amount_claimed', '0'), filters.get('amount', '')):
        return False
    if not date_matches(detail.get('operation_date') or appeal.created_at, filters.get('date', '')):
        return False
    requisite_query = filters.get('requisite', '')
    if requisite_query:
        requisite_text = ' '.join([
            str(detail.get('requisite') or ''),
            str(detail.get('recipient_bank') or ''),
        ])
        if not text_contains(requisite_text, requisite_query):
            return False
    return True


APPEAL_UPLOAD_ROOT = Path('uploads/appeals')
APPEAL_ALLOWED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.pdf', '.webp'}
APPEAL_DANGEROUS_EXTENSIONS = {
    '.bat', '.cmd', '.com', '.dll', '.exe', '.hta', '.html', '.js', '.jsp',
    '.php', '.phtml', '.ps1', '.py', '.scr', '.sh', '.svg', '.vbs',
}
APPEAL_MAX_UPLOAD_BYTES = 5 * 1024 * 1024


def _redirect(message: str, section: str = '') -> RedirectResponse:
    return RedirectResponse(
        cabinet_redirect_url('/cabinet', message=message, section=section),
        status_code=303,
    )


def _success_redirect(
    section: str = '',
    *,
    base_path: str = '/cabinet',
    extra: dict[str, str] | None = None,
    fragment: str = '',
) -> RedirectResponse:
    return RedirectResponse(
        cabinet_redirect_url(
            base_path,
            section=section,
            success=True,
            extra=extra,
            fragment=fragment,
        ),
        status_code=303,
    )


def _appeal_file_info_url(file_info: dict | None, cabinet_base: str = '/cabinet') -> str:
    if not file_info:
        return ''
    path = str(file_info.get('path') or '')
    return f'{cabinet_base}/appeals/files/{path}' if path else ''


async def save_appeal_upload(upload: UploadFile | None, *, required: bool) -> dict | None:
    if not upload or not upload.filename:
        if required:
            raise ValueError('Приложите файл')
        return None
    raw_filename = str(upload.filename)
    if '/' in raw_filename or '\\' in raw_filename or '..' in raw_filename:
        raise ValueError('invalid file name')
    original_name = Path(raw_filename).name
    ext = Path(original_name).suffix.lower()
    if ext in APPEAL_DANGEROUS_EXTENSIONS or ext not in APPEAL_ALLOWED_EXTENSIONS:
        raise ValueError('Разрешены только JPG, PNG, WEBP или PDF')
    content = await upload.read()
    if not content:
        if required:
            raise ValueError('Файл пустой')
        return None
    if len(content) > APPEAL_MAX_UPLOAD_BYTES:
        raise ValueError('Файл больше 5 МБ')
    if not _appeal_upload_magic_matches(ext, content):
        raise ValueError('file type does not match extension')
    APPEAL_UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    stored_name = f'{uuid.uuid4().hex}{ext}'
    (APPEAL_UPLOAD_ROOT / stored_name).write_bytes(content)
    return {
        'path': stored_name,
        'original_name': original_name[:180],
        'content_type': upload.content_type or 'application/octet-stream',
        'size': len(content),
    }


def _appeal_upload_magic_matches(ext: str, content: bytes) -> bool:
    if ext == '.pdf':
        return content.startswith(b'%PDF-')
    if ext in {'.jpg', '.jpeg'}:
        return content.startswith(b'\xff\xd8\xff')
    if ext == '.png':
        return content.startswith(b'\x89PNG\r\n\x1a\n')
    if ext == '.webp':
        return len(content) >= 12 and content[:4] == b'RIFF' and content[8:12] == b'WEBP'
    return False


def _safe_appeal_file_path(filename: str) -> Path:
    clean = Path(filename).name
    if clean != filename:
        raise ValueError('invalid file path')
    ext = Path(clean).suffix.lower()
    if ext in APPEAL_DANGEROUS_EXTENSIONS or ext not in APPEAL_ALLOWED_EXTENSIONS:
        raise ValueError('invalid file extension')
    uuid.UUID(Path(clean).stem)
    path = (APPEAL_UPLOAD_ROOT / clean).resolve()
    root = APPEAL_UPLOAD_ROOT.resolve()
    if root not in path.parents:
        raise ValueError('invalid file path')
    return path


async def _deposit_trader_id(db: AsyncSession, dep: Deposit) -> str | None:
    if not dep.requisites_id:
        return None
    req = (await db.execute(select(Requisite).where(Requisite.id == dep.requisites_id))).scalar_one_or_none()
    return str(req.trader_id) if req and req.trader_id else None


async def _appeal_visible_to_user(db: AsyncSession, user: User, appeal: Appeal) -> bool:
    return await can_view_appeal(db, user, appeal)


async def _merchant_deposit_by_lookup(db: AsyncSession, merchant: Merchant, lookup: str, *, for_update: bool = False) -> Deposit | None:
    lookup = (lookup or '').strip()
    if not lookup:
        return None
    stmt = select(Deposit).where(Deposit.merchant_id == merchant.id)
    try:
        dep_id = uuid.UUID(lookup)
        stmt = stmt.where(Deposit.id == dep_id)
    except ValueError:
        stmt = stmt.where(Deposit.external_id == lookup)
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def _appeal_file_belongs_to_visible_appeal(db: AsyncSession, user: User, filename: str) -> bool:
    appeals = (await db.execute(select(Appeal).order_by(Appeal.created_at.desc()).limit(1000))).scalars().all()
    for appeal in appeals:
        meta = appeal_metadata(appeal)
        files = [meta.get('receipt_file'), meta.get('statement_file')]
        if filename not in {str((item or {}).get('path') or '') for item in files}:
            continue
        if await _appeal_visible_to_user(db, user, appeal):
            return True
    return False


def _clamp_percent(value: str | Decimal | int | None, default: str = '7') -> Decimal:
    pct = safe_decimal(str(value if value is not None else default), default)
    return max(Decimal('0.00'), min(Decimal('100.00'), pct))


def _user_can_be_managed_by_superadmin(target: User, actor: User) -> bool:
    return actor.role == Role.superadmin.value and target.role in MANAGED_USER_ROLES and str(target.id) != str(actor.id)

def merchant_credentials_response(
    request: Request,
    actor: User,
    merchant: Merchant,
    merchant_email: str,
    api_key: str,
    secret_key: str,
    mode: str,
    login_password: str | None = None,
    title: str = 'Merchant API credentials',
    credential_kind: str = 'merchant_api',
):
    realm = request_auth_realm(request) or realm_for_role(actor.role) or 'staff'
    response = templates.TemplateResponse(
        request=request,
        name='merchant_credentials.html',
        context={
            'request': request,
            'user': actor,
            **branding_context(),
            'title': title,
            'merchant': merchant,
            'merchant_email': merchant_email,
            'api_key': api_key,
            'secret_key': secret_key,
            'login_password': login_password,
            'mode': api_key_mode_label(mode),
            'credential_kind': credential_kind,
            'cabinet_base': realm_cabinet_path(realm),
        },
    )
    response.headers['Cache-Control'] = 'no-store, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    return response


@router.post(
    '/cabinet/merchants/{merchant_id}/webhook-signing-keys/issue',
    response_class=HTMLResponse,
)
async def issue_webhook_signing_key_from_cabinet(
    request: Request,
    merchant_id: str,
    db: AsyncSession = Depends(get_db),
):
    actor = await get_current_web_user(request, db)
    if not actor:
        return RedirectResponse('/login', status_code=303)
    if staff_2fa_setup_required(actor):
        return _twofa_redirect()
    if actor.role != Role.superadmin.value:
        return _merchant_key_redirect(
            request,
            'Only Superadmin can issue webhook signing keys.',
        )
    merchant = await _locked_merchant(db, merchant_id)
    if not merchant:
        return _merchant_key_redirect(request, 'Merchant not found.')
    try:
        key, secret = await issue_webhook_signing_key(
            db,
            merchant,
            created_by=actor.id,
        )
    except WebhookSigningKeyError as exc:
        return _merchant_key_redirect(request, str(exc))
    owner = await db.scalar(
        select(User).where(User.id == merchant.owner_id)
    )
    await audit(
        db,
        'merchant_webhook_signing_key_issued',
        'merchant_webhook_signing_key',
        actor.id,
        key.id,
        client_ip(request),
        {
            'merchant_id': str(merchant.id),
            'key_fingerprint': webhook_key_fingerprint(key.key_id),
        },
    )
    await db.commit()
    return merchant_credentials_response(
        request=request,
        actor=actor,
        merchant=merchant,
        merchant_email=owner.email if owner else '',
        api_key=key.key_id,
        secret_key=secret,
        mode='production',
        title='New webhook signing credentials',
        credential_kind='webhook_signing',
    )


@router.post(
    '/cabinet/merchants/{merchant_id}/webhook-signing-keys/rotate',
    response_class=HTMLResponse,
)
async def rotate_webhook_signing_key_from_cabinet(
    request: Request,
    merchant_id: str,
    db: AsyncSession = Depends(get_db),
):
    actor = await get_current_web_user(request, db)
    if not actor:
        return RedirectResponse('/login', status_code=303)
    if staff_2fa_setup_required(actor):
        return _twofa_redirect()
    if actor.role != Role.superadmin.value:
        return _merchant_key_redirect(
            request,
            'Only Superadmin can rotate webhook signing keys.',
        )
    merchant = await _locked_merchant(db, merchant_id)
    if not merchant:
        return _merchant_key_redirect(request, 'Merchant not found.')
    try:
        key, secret, retiring = await rotate_webhook_signing_key(
            db,
            merchant,
            created_by=actor.id,
        )
    except WebhookSigningKeyError as exc:
        return _merchant_key_redirect(request, str(exc))
    owner = await db.scalar(
        select(User).where(User.id == merchant.owner_id)
    )
    await audit(
        db,
        'merchant_webhook_signing_key_rotated',
        'merchant_webhook_signing_key',
        actor.id,
        key.id,
        client_ip(request),
        {
            'merchant_id': str(merchant.id),
            'new_key_fingerprint': webhook_key_fingerprint(key.key_id),
            'retiring_key_fingerprint': webhook_key_fingerprint(
                retiring.key_id
            ),
            'retire_at': retiring.retire_at.isoformat(),
        },
    )
    await db.commit()
    return merchant_credentials_response(
        request=request,
        actor=actor,
        merchant=merchant,
        merchant_email=owner.email if owner else '',
        api_key=key.key_id,
        secret_key=secret,
        mode='production',
        title='Rotated webhook signing credentials',
        credential_kind='webhook_signing',
    )


@router.post('/cabinet/webhooks/{event_id}/retry')
async def retry_webhook_from_cabinet(
    request: Request,
    event_id: str,
    db: AsyncSession = Depends(get_db),
):
    actor = await get_current_web_user(request, db)
    if not actor:
        return RedirectResponse('/login', status_code=303)
    if staff_2fa_setup_required(actor):
        return _twofa_redirect()
    if actor.role != Role.superadmin.value:
        return _redirect(
            'Manual webhook retry is restricted to Superadmin.',
            'webhook',
        )
    event = await request_manual_webhook_retry(db, event_id)
    if not event:
        return _redirect('Webhook event not found.', 'webhook')
    await audit(
        db,
        'webhook_retry_requested',
        'webhook_event',
        actor.id,
        event.id,
        client_ip(request),
        {
            'event_id': str(event.id),
            'attempts': event.attempts,
            'max_attempts': event.max_attempts,
        },
    )
    await db.commit()
    enqueued = (
        False
        if event.status == 'delivered'
        else enqueue_webhook_delivery(event.id)
    )
    return _redirect(
        (
            'Webhook retry queued.'
            if enqueued
            else 'Webhook retry recorded; scanner will recover enqueue.'
        ),
        'webhook',
    )


async def get_current_web_user(request: Request, db: AsyncSession):
    uid = get_session_user_id(request)
    if not uid:
        _mark_session_event(request, 'missing_session_user')
        return None
    realm = request_auth_realm(request)
    if realm and request.session.get(SESSION_REALM_KEY) != realm:
        _mark_session_event(request, 'session_realm_mismatch')
        request.session.clear()
        return None
    if await _session_identity_changed(request):
        _mark_session_event(request, SESSION_IDENTITY_CHANGED_CODE)
        raise HTTPException(
            status_code=409,
            detail={
                'code': SESSION_IDENTITY_CHANGED_CODE,
                'message': SESSION_IDENTITY_CHANGED_MESSAGE,
            },
        )
    user = (await db.execute(select(User).where(User.id == uid))).scalar_one_or_none()
    if not user or not user.is_active or user.is_locked or getattr(user, 'is_archived', False):
        _mark_session_event(request, 'user_missing_inactive_or_locked')
        request.session.clear()
        return None
    if request.session.get('auth') != _session_marker(user):
        _mark_session_event(request, 'auth_state_mismatch')
        request.session.clear()
        return None
    if realm and (realm_for_role(user.role) != realm or request.session.get('role') != user.role):
        _mark_session_event(request, 'session_role_realm_mismatch')
        request.session.clear()
        return None
    return user


def login_context(request: Request, error: str | None = None):
    expected_realm = request.scope.get('expected_auth_realm')
    return {
        'request': request,
        'error': error,
        'expected_realm': expected_realm,
        'login_action': realm_login_path(expected_realm) if expected_realm else '/login',
        **branding_context(),
    }


@router.get('/login', response_class=HTMLResponse)
async def login_page(request: Request, error: str | None = None):
    return templates.TemplateResponse(
        request=request,
        name='login.html',
        context=login_context(request, error),
    )


@router.post('/login')
async def login_submit(request: Request, email: str = Form(...), password: str = Form(...), otp: str = Form(''), db: AsyncSession = Depends(get_db)):
    retry_after = await hit_rate_limit('web-login', request, email, settings.LOGIN_RATE_LIMIT)
    if retry_after:
        return templates.TemplateResponse(
            request=request,
            name='login.html',
            context=login_context(
                request,
                'Слишком много попыток входа. Повторите позже.',
            ),
            status_code=429,
        )
    email = email.strip().lower()
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    ip = client_ip(request)
    if user and await is_auth_temporarily_locked(user, 'login'):
        await audit(db, 'account_locked', 'user', actor_id=user.id, ip=ip, details={'scope': 'login'})
        await db.commit()
        return templates.TemplateResponse(
            request=request,
            name='login.html',
            context=login_context(
                request,
                'Слишком много попыток входа. Повторите позже.',
            ),
            status_code=429,
        )
    password_ok = bool(user and user.is_active and not user.is_locked and verify_password(password, user.password_hash))
    if not password_ok:
        if user and user.is_active and not user.is_locked:
            locked = await record_auth_failure(user, 'password', 'login')
            if locked:
                await audit(db, 'account_locked', 'user', actor_id=user.id, ip=ip, details={'scope': 'login'})
        await audit(db, 'failed_login', 'user', target_id=email, ip=ip)
        await db.commit()
        return templates.TemplateResponse(
            request=request,
            name='login.html',
            context=login_context(request, 'Неверный email или пароль'),
            status_code=401,
        )
    if user.twofa_enabled:
        if await is_auth_temporarily_locked(user, '2fa'):
            await audit(db, 'account_locked', 'user', actor_id=user.id, ip=ip, details={'scope': '2fa'})
            await db.commit()
            return templates.TemplateResponse(
                request=request,
                name='login.html',
                context=login_context(
                    request,
                    'Слишком много попыток входа. Повторите позже.',
                ),
                status_code=429,
            )
        if not user.twofa_secret or not otp or not pyotp.TOTP(decrypt_secret(user.twofa_secret)).verify(otp, valid_window=1):
            locked = await record_auth_failure(user, '2fa', '2fa')
            await audit(db, '2fa_failed', 'user', actor_id=user.id, ip=ip)
            if locked:
                await audit(db, 'account_locked', 'user', actor_id=user.id, ip=ip, details={'scope': '2fa'})
            await db.commit()
            return templates.TemplateResponse(
                request=request,
                name='login.html',
                context=login_context(
                    request,
                    'Нужен корректный 2FA код',
                ),
                status_code=401,
            )
        await audit(db, '2fa_success', 'user', actor_id=user.id, ip=ip)
    realm = realm_for_role(user.role)
    expected_realm = request.scope.get('expected_auth_realm')
    if not realm or (expected_realm and expected_realm != realm):
        await audit(db, 'web_login_realm_rejected', 'user', actor_id=user.id, ip=ip, details={'expected_realm': expected_realm or ''})
        await db.commit()
        return templates.TemplateResponse(
            request=request,
            name='login.html',
            context=login_context(
                request,
                'Эта учётная запись относится к другому кабинету.',
            ),
            status_code=403,
        )
    await record_auth_success(user, 'login')
    realm_session = activate_realm_session(request, realm)
    previous_uid = realm_session.get('user_id')
    realm_session.clear()
    realm_session['user_id'] = str(user.id)
    realm_session['role'] = user.role
    realm_session['auth'] = _session_marker(user)
    realm_session['twofa_verified'] = bool(user.twofa_enabled)
    realm_session[SESSION_REALM_KEY] = realm
    realm_session[SESSION_GENERATION_KEY] = secrets.token_urlsafe(24)
    _mark_session_event(
        request,
        f'{realm}_login_identity_replaced' if previous_uid and str(previous_uid) != str(user.id) else f'{realm}_login_success',
    )
    await audit(db, 'successful_login', 'user', actor_id=user.id, ip=ip)
    await db.commit()
    return RedirectResponse(realm_cabinet_path(realm), status_code=303)


async def _complete_logout(request: Request, db: AsyncSession) -> RedirectResponse:
    realm = request_auth_realm(request)
    if await _session_identity_changed(request):
        _mark_session_event(request, SESSION_IDENTITY_CHANGED_CODE)
        cabinet_path = realm_cabinet_path(realm) if realm else '/cabinet'
        return RedirectResponse(cabinet_path + '?session_view=' + quote_plus(await _request_session_view(request)), status_code=303)
    uid = request.session.get('user_id')
    try:
        if uid:
            try:
                actor_id = uuid.UUID(str(uid))
            except (TypeError, ValueError):
                actor_id = None
            if actor_id:
                await audit(db, 'logout', 'user', actor_id=actor_id, ip=client_ip(request))
                await db.commit()
    except Exception:
        try:
            await db.rollback()
        except Exception:
            security_logger.exception('logout_audit_rollback_failed')
        security_logger.warning(
            'logout_audit_failed',
            extra={'actor_id': str(uid or ''), 'client_ip': client_ip(request)},
            exc_info=True,
        )
    finally:
        _mark_session_event(request, f'{realm}_logout' if realm else 'logout_without_realm')
        request.session.clear()
    return RedirectResponse(realm_login_path(realm) if realm else '/login', status_code=303)


@router.get('/logout')
async def logout_get(request: Request, db: AsyncSession = Depends(get_db)):
    return await _complete_logout(request, db)


@router.post('/logout')
async def logout(request: Request, db: AsyncSession = Depends(get_db)):
    return await _complete_logout(request, db)


@router.get('/', response_class=HTMLResponse)
async def index(request: Request, db: AsyncSession = Depends(get_db)):
    sessions = request.scope.get('auth_sessions') or {}
    authenticated_realms = [realm for realm, session in sessions.items() if session.get('user_id')]
    if len(authenticated_realms) == 1:
        return RedirectResponse(realm_cabinet_path(authenticated_realms[0]), status_code=303)
    return RedirectResponse('/login', status_code=303)


@router.get('/pay/{merchant_id}', response_class=HTMLResponse)
async def player_pay_page(merchant_id: str):
    raise HTTPException(status_code=404, detail='payment page disabled')


@router.post('/pay/{merchant_id}', response_class=HTMLResponse)
async def player_pay_submit(merchant_id: str):
    raise HTTPException(status_code=404, detail='payment page disabled')


@router.post('/cabinet/users/create', response_class=HTMLResponse)
async def create_user_from_cabinet(request: Request, email: str = Form(...), password: str = Form(...), role: str = Form(...), merchant_name: str = Form(''), db: AsyncSession = Depends(get_db)):
    actor = await get_current_web_user(request, db)
    if not actor:
        return RedirectResponse('/login', status_code=303)
    if staff_2fa_setup_required(actor):
        return _twofa_redirect()
    allowed = []
    if actor.role == Role.superadmin.value:
        allowed = [Role.admin.value, Role.support.value, Role.teamlead.value, Role.merchant.value, Role.operator.value, Role.trader.value]
    elif actor.role == Role.admin.value:
        allowed = [Role.support.value, Role.teamlead.value, Role.merchant.value, Role.operator.value, Role.trader.value]
    if role not in allowed:
        return _redirect('permission_denied', 'create')
    email = email.strip().lower()
    exists = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if exists:
        return _redirect('email_exists', 'create')
    user = User(email=email, password_hash=hash_password(password), role=role, twofa_enabled=False, twofa_secret=None)
    db.add(user)
    await db.flush()
    api_key = ''
    secret_key = ''
    if role == Role.merchant.value:
        m = Merchant(owner_id=user.id, name=merchant_name or email, webhook_url=None, ip_whitelist=[], sandbox_mode=True, merchant_commission_percent=Decimal('15.00'))
        db.add(m)
        await db.flush()
        api_key = 'pk_' + secrets.token_urlsafe(24)
        secret_key = 'sk_' + secrets.token_urlsafe(32)
        db.add(ApiKey(merchant_id=m.id, api_key=api_key, secret_hash=encrypt_secret(secret_key), mode='sandbox'))
        db.add(Balance(merchant_id=m.id, available=Decimal('0.00'), frozen=Decimal('0.00')))
    await audit(db, 'cabinet_user_created', 'user', actor.id, user.id, client_ip(request), {'role': role, 'merchant_api_key_created': bool(api_key)})
    await db.commit()
    if api_key:
        return merchant_credentials_response(
            request=request,
            actor=actor,
            merchant=m,
            merchant_email=email,
            api_key=api_key,
            secret_key=secret_key,
            mode='sandbox',
            login_password=password,
            title='New merchant credentials',
        )
    return _success_redirect('create')


@router.post('/cabinet/users/{user_id}/password')
async def reset_user_password_from_cabinet(request: Request, user_id: str, new_password: str = Form(...), db: AsyncSession = Depends(get_db)):
    actor = await get_current_web_user(request, db)
    if not actor:
        return RedirectResponse('/login', status_code=303)
    if staff_2fa_setup_required(actor):
        return _twofa_redirect()
    if actor.role != Role.superadmin.value:
        return _redirect('permission_denied', 'users')
    if len(new_password) < 10:
        return _redirect('password_too_short', 'users')
    try:
        target_uuid = uuid.UUID(user_id)
    except ValueError:
        return _redirect('user_not_found', 'users')
    target = (await db.execute(select(User).where(User.id == target_uuid))).scalar_one_or_none()
    if not target or not _user_can_be_managed_by_superadmin(target, actor):
        return _redirect('user_protected', 'users')
    target.password_hash = hash_password(new_password)
    target.failed_login_count = 0
    await revoke_refresh_tokens(db, target.id)
    await audit(
        db,
        'cabinet_user_password_reset',
        'user',
        actor_id=actor.id,
        target_id=str(target.id),
        ip=client_ip(request),
        details={'target_email': target.email, 'target_role': target.role},
    )
    await db.commit()
    return _success_redirect('users')


@router.post('/cabinet/users/{user_id}/lock')
async def lock_user_from_cabinet(request: Request, user_id: str, locked: str = Form(...), db: AsyncSession = Depends(get_db)):
    actor = await get_current_web_user(request, db)
    if not actor:
        return RedirectResponse('/login', status_code=303)
    if staff_2fa_setup_required(actor):
        return _twofa_redirect()
    if actor.role != Role.superadmin.value:
        return _redirect('permission_denied', 'users')
    try:
        target_uuid = uuid.UUID(user_id)
    except ValueError:
        return _redirect('user_not_found', 'users')
    target = (await db.execute(select(User).where(User.id == target_uuid))).scalar_one_or_none()
    if not target or not _user_can_be_managed_by_superadmin(target, actor):
        return _redirect('user_protected', 'users')
    should_lock = locked == 'true'
    target.is_locked = should_lock
    target.failed_login_count = 0 if not should_lock else target.failed_login_count
    if should_lock:
        await revoke_refresh_tokens(db, target.id)
    await audit(
        db,
        'cabinet_user_locked' if should_lock else 'cabinet_user_unlocked',
        'user',
        actor_id=actor.id,
        target_id=str(target.id),
        ip=client_ip(request),
        details={'target_email': target.email, 'target_role': target.role},
    )
    await db.commit()
    return _success_redirect('users')

@router.post('/cabinet/merchants/{merchant_id}/integration')
async def update_merchant_integration(
    request: Request,
    merchant_id: str,
    webhook_url: str = Form(''),
    ip_whitelist: str = Form(''),
    sandbox_mode: str = Form('off'),
    merchant_commission_percent: str = Form(''),
    db: AsyncSession = Depends(get_db),
):
    actor = await get_current_web_user(request, db)
    if not actor:
        return RedirectResponse('/login', status_code=303)
    if staff_2fa_setup_required(actor):
        return _twofa_redirect()
    if actor.role not in [Role.superadmin.value, Role.admin.value, Role.merchant.value]:
        return _redirect('permission_denied', 'merchants')
    try:
        merchant_uuid = uuid.UUID(merchant_id)
    except ValueError:
        return _redirect('merchant_not_found', 'merchants')
    merchant = (await db.execute(select(Merchant).where(Merchant.id == merchant_uuid))).scalar_one_or_none()
    if not merchant:
        return _redirect('merchant_not_found', 'merchants')
    if actor.role == Role.merchant.value and actor.id != merchant.owner_id:
        return _redirect('permission_denied', 'merchants')
    try:
        merchant.webhook_url = validate_public_webhook_url(webhook_url.strip() or None)
    except ValueError:
        return _redirect('webhook_url_invalid', 'merchants')
    try:
        merchant.ip_whitelist = validate_ip_whitelist([item.strip() for item in ip_whitelist.split(',') if item.strip()])
    except ValueError:
        return _redirect('ip_whitelist_invalid', 'merchants')
    # Mode changes require explicit Production authorization, never a checkbox.
    if actor.role == Role.superadmin.value and merchant_commission_percent.strip():
        merchant.merchant_commission_percent = _clamp_percent(merchant_commission_percent, '15')
    await audit(db, 'merchant_integration_updated', 'merchant', actor.id, merchant.id, client_ip(request), {'webhook_configured': bool(merchant.webhook_url), 'ip_count': len(merchant.ip_whitelist), 'sandbox_mode': merchant.sandbox_mode})
    await db.commit()
    return _success_redirect('merchants')

@router.post('/cabinet/merchants/{merchant_id}/commission')
async def update_merchant_commission(request: Request, merchant_id: str, merchant_commission_percent: str = Form(...), db: AsyncSession = Depends(get_db)):
    actor = await get_current_web_user(request, db)
    if not actor:
        return RedirectResponse('/login', status_code=303)
    if staff_2fa_setup_required(actor):
        return _twofa_redirect()
    if actor.role != Role.superadmin.value:
        return _redirect('permission_denied', 'merchants')
    try:
        merchant_uuid = uuid.UUID(merchant_id)
    except ValueError:
        return _redirect('platform_not_found', 'merchants')
    merchant = (await db.execute(select(Merchant).where(Merchant.id == merchant_uuid).with_for_update())).scalar_one_or_none()
    if not merchant:
        return _redirect('platform_not_found', 'merchants')
    merchant.merchant_commission_percent = _clamp_percent(merchant_commission_percent, '15')
    await audit(db, 'merchant_commission_updated', 'merchant', actor.id, merchant.id, client_ip(request), {'merchant_commission_percent': str(merchant.merchant_commission_percent)})
    await db.commit()
    return _success_redirect('merchants')
def _merchant_key_redirect(
    request: Request,
    message: str,
    *,
    success: bool = False,
) -> RedirectResponse:
    realm = request_auth_realm(request) or 'staff'
    return RedirectResponse(
        cabinet_redirect_url(
            realm_cabinet_path(realm),
            message=message,
            section='merchants',
            success=success,
        ),
        status_code=303,
    )


async def _merchant_key_actor(request: Request, db: AsyncSession) -> User | None:
    actor = await get_current_web_user(request, db)
    if not actor:
        return None
    if staff_2fa_setup_required(actor):
        raise HTTPException(403, '2FA setup required')
    if not can_manage_merchant_api_keys(actor.role):
        raise HTTPException(403, 'Only superadmin/admin can manage merchant API keys')
    return actor


async def _locked_merchant(db: AsyncSession, merchant_id: str) -> Merchant | None:
    try:
        merchant_uuid = uuid.UUID(merchant_id)
    except ValueError:
        return None
    return (await db.execute(
        select(Merchant).where(Merchant.id == merchant_uuid).with_for_update()
    )).scalar_one_or_none()


@router.post('/cabinet/merchants/{merchant_id}/production/{action}')
async def manage_merchant_production(request: Request, merchant_id: UUID, action: str,
    confirmation: str = Form(...), reason: str = Form(...), db: AsyncSession = Depends(get_db)):
    from app.services.integration_modes import change_production_access, IntegrationModeError
    actor = await _require_superadmin_web(request, db)
    if isinstance(actor, RedirectResponse):
        return actor
    try:
        await change_production_access(db, merchant_id, actor, action=action,
            confirmation=confirmation, reason=reason, ip=client_ip(request))
    except IntegrationModeError as exc:
        return _merchant_key_redirect(request, str(exc) + ' ' + ' '.join(r['message'] for r in exc.reasons))
    await db.commit()
    return RedirectResponse('/staff/cabinet/tradespace/integrations?tab=credentials&id=' + str(merchant_id), status_code=303)


@router.post('/cabinet/merchants/{merchant_id}/api-keys/issue', response_class=HTMLResponse)
async def issue_merchant_api_key_from_cabinet(
    request: Request,
    merchant_id: str,
    mode: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    try:
        actor = await _merchant_key_actor(request, db)
    except HTTPException as exc:
        return _merchant_key_redirect(request, str(exc.detail))
    if not actor:
        return RedirectResponse('/login', status_code=303)
    merchant = await _locked_merchant(db, merchant_id)
    if not merchant:
        return _merchant_key_redirect(request, 'Площадка не найдена')
    try:
        key, secret_key = await issue_merchant_api_key(db, merchant, mode)
    except MerchantApiKeyError as exc:
        return _merchant_key_redirect(request, str(exc))
    merchant_owner = (await db.execute(select(User).where(User.id == merchant.owner_id))).scalar_one_or_none()
    await audit(
        db,
        'merchant_api_key_issued',
        'api_key',
        actor_id=actor.id,
        target_id=str(key.id),
        ip=client_ip(request),
        details={'merchant_id': str(merchant.id), 'mode': key.mode, 'key_fingerprint': api_key_fingerprint(key.api_key)},
    )
    await db.commit()
    return merchant_credentials_response(
        request=request,
        actor=actor,
        merchant=merchant,
        merchant_email=merchant_owner.email if merchant_owner else '',
        api_key=key.api_key,
        secret_key=secret_key,
        mode=key.mode,
        title='New merchant API credentials',
    )


@router.post('/cabinet/merchants/{merchant_id}/api-keys/{key_id}/rotate', response_class=HTMLResponse)
async def rotate_merchant_api_key_from_cabinet(
    request: Request,
    merchant_id: str,
    key_id: str,
    db: AsyncSession = Depends(get_db),
):
    try:
        actor = await _merchant_key_actor(request, db)
    except HTTPException as exc:
        return _merchant_key_redirect(request, str(exc.detail))
    if not actor:
        return RedirectResponse('/login', status_code=303)
    merchant = await _locked_merchant(db, merchant_id)
    try:
        key_uuid = uuid.UUID(key_id)
    except ValueError:
        key_uuid = None
    key = (await db.execute(
        select(ApiKey).where(
            ApiKey.id == key_uuid,
            ApiKey.merchant_id == merchant.id if merchant else False,
        ).with_for_update()
    )).scalar_one_or_none() if key_uuid and merchant else None
    if not merchant or not key:
        return _merchant_key_redirect(request, 'API key не найден')
    try:
        new_key, secret_key = await rotate_merchant_api_key(db, merchant, key)
    except MerchantApiKeyError as exc:
        return _merchant_key_redirect(request, str(exc))
    merchant_owner = (await db.execute(select(User).where(User.id == merchant.owner_id))).scalar_one_or_none()
    await audit(
        db,
        'merchant_api_key_rotated',
        'api_key',
        actor_id=actor.id,
        target_id=str(new_key.id),
        ip=client_ip(request),
        details={
            'merchant_id': str(merchant.id),
            'mode': new_key.mode,
            'replaced_key_id': str(key.id),
            'key_fingerprint': api_key_fingerprint(new_key.api_key),
        },
    )
    await db.commit()
    return merchant_credentials_response(
        request=request,
        actor=actor,
        merchant=merchant,
        merchant_email=merchant_owner.email if merchant_owner else '',
        api_key=new_key.api_key,
        secret_key=secret_key,
        mode=new_key.mode,
        title='Rotated merchant API credentials',
    )


@router.post('/cabinet/merchants/{merchant_id}/api-keys/{key_id}/revoke')
async def revoke_merchant_api_key_from_cabinet(
    request: Request,
    merchant_id: str,
    key_id: str,
    db: AsyncSession = Depends(get_db),
):
    try:
        actor = await _merchant_key_actor(request, db)
    except HTTPException as exc:
        return _merchant_key_redirect(request, str(exc.detail))
    if not actor:
        return RedirectResponse('/login', status_code=303)
    merchant = await _locked_merchant(db, merchant_id)
    try:
        key_uuid = uuid.UUID(key_id)
    except ValueError:
        key_uuid = None
    key = (await db.execute(
        select(ApiKey).where(
            ApiKey.id == key_uuid,
            ApiKey.merchant_id == merchant.id if merchant else False,
        ).with_for_update()
    )).scalar_one_or_none() if key_uuid and merchant else None
    if not merchant or not key:
        return _merchant_key_redirect(request, 'API key не найден')
    try:
        await revoke_merchant_api_key(db, key)
    except MerchantApiKeyError as exc:
        return _merchant_key_redirect(request, str(exc))
    await audit(
        db,
        'merchant_api_key_revoked',
        'api_key',
        actor_id=actor.id,
        target_id=str(key.id),
        ip=client_ip(request),
        details={'merchant_id': str(merchant.id), 'mode': normalize_api_key_mode(key.mode), 'key_fingerprint': api_key_fingerprint(key.api_key)},
    )
    await db.commit()
    return _merchant_key_redirect(
        request,
        f'{api_key_mode_label(key.mode)} API key отозван',
        success=True,
    )


def _aggregator_redirect(message: str, *, success: bool = False) -> RedirectResponse:
    return RedirectResponse(
        cabinet_redirect_url(
            '/cabinet',
            message=message,
            section='aggregators',
            success=success,
        ),
        status_code=303,
    )


def _optional_url(value: str) -> str | None:
    value = (value or '').strip()
    return validate_public_webhook_url(value) if value else None


async def _get_aggregator_for_update(db: AsyncSession, aggregator_id: str) -> AggregatorAccount | None:
    try:
        account_id = uuid.UUID(aggregator_id)
    except ValueError:
        return None
    return (await db.execute(
        select(AggregatorAccount).where(AggregatorAccount.id == account_id).with_for_update()
    )).scalar_one_or_none()


@router.post('/cabinet/aggregators/create')
async def cabinet_create_aggregator(
    request: Request,
    name: str = Form(...),
    callback_url: str = Form(''),
    success_url: str = Form(''),
    fail_url: str = Form(''),
    commission_percent: str = Form(''),
    min_payment_amount: str = Form('100'),
    max_payment_amount: str = Form('150000'),
    daily_limit: str = Form(''),
    monthly_limit: str = Form(''),
    db: AsyncSession = Depends(get_db),
):
    actor = await get_current_web_user(request, db)
    if not actor:
        return RedirectResponse('/login', status_code=303)
    if staff_2fa_setup_required(actor):
        return _twofa_redirect()
    if actor.role not in [Role.superadmin.value, Role.admin.value]:
        return _aggregator_redirect('Not enough permissions')
    try:
        account, secret = await create_aggregator_account(
            db,
            name=name.strip(),
            callback_url=_optional_url(callback_url),
            success_url=_optional_url(success_url),
            fail_url=_optional_url(fail_url),
            commission_percent=safe_decimal(commission_percent, '0'),
            min_payment_amount=safe_decimal(min_payment_amount, '100'),
            max_payment_amount=safe_decimal(max_payment_amount, '150000'),
            daily_limit=safe_decimal(daily_limit, '0') if daily_limit.strip() else None,
            monthly_limit=safe_decimal(monthly_limit, '0') if monthly_limit.strip() else None,
        )
    except Exception as exc:
        await db.rollback()
        return _aggregator_redirect(str(exc))
    secret_flash_available = bool(secret) and await _store_aggregator_secret_flash(request, actor, {
        'name': account.name, 'api_key': account.api_key, 'secret_key': secret, 'mode':'sandbox',
    })
    await audit(
        db,
        'cabinet_aggregator_created',
        'aggregator',
        actor.id,
        account.id,
        client_ip(request),
        {'name': account.name, 'secret_flash_available': secret_flash_available},
    )
    await db.commit()
    if not secret:
        return _aggregator_redirect('Агрегатор создан. Production-ключ выпускается отдельно Superadmin после разрешения владельца.', success=True)
    message = 'Aggregator created. Secret key is shown once below.' if secret_flash_available else AGGREGATOR_SECRET_FLASH_UNAVAILABLE_MESSAGE
    return _aggregator_redirect(message, success=secret_flash_available)


@router.post('/cabinet/aggregators/{aggregator_id}/update')
async def cabinet_update_aggregator(
    request: Request,
    aggregator_id: str,
    name: str = Form(...),
    callback_url: str = Form(''),
    success_url: str = Form(''),
    fail_url: str = Form(''),
    commission_percent: str = Form(''),
    min_payment_amount: str = Form('100'),
    max_payment_amount: str = Form('150000'),
    daily_limit: str = Form(''),
    monthly_limit: str = Form(''),
    db: AsyncSession = Depends(get_db),
):
    actor = await get_current_web_user(request, db)
    if not actor:
        return RedirectResponse('/login', status_code=303)
    if staff_2fa_setup_required(actor):
        return _twofa_redirect()
    if actor.role not in [Role.superadmin.value, Role.admin.value]:
        return _aggregator_redirect('Not enough permissions')
    account = await _get_aggregator_for_update(db, aggregator_id)
    if not account:
        return _aggregator_redirect('Aggregator not found')
    try:
        account.name = name.strip()
        account.callback_url = _optional_url(callback_url)
        account.success_url = _optional_url(success_url)
        account.fail_url = _optional_url(fail_url)
        if commission_percent.strip():
            account.commission_percent = percent(safe_decimal(commission_percent, '0'))
        account.min_payment_amount = money(safe_decimal(min_payment_amount, '100'))
        account.max_payment_amount = money(safe_decimal(max_payment_amount, '150000'))
        account.daily_limit = money(safe_decimal(daily_limit, '0')) if daily_limit.strip() else None
        account.monthly_limit = money(safe_decimal(monthly_limit, '0')) if monthly_limit.strip() else None
        if money(account.max_payment_amount) < money(account.min_payment_amount):
            raise ValueError('Max payment amount cannot be lower than min payment amount')
    except Exception as exc:
        await db.rollback()
        return _aggregator_redirect(str(exc))
    await audit(db, 'cabinet_aggregator_updated', 'aggregator', actor.id, account.id, client_ip(request), {'name': account.name})
    await db.commit()
    return _aggregator_redirect('Aggregator settings saved', success=True)


@router.post('/cabinet/aggregators/{aggregator_id}/status')
async def cabinet_update_aggregator_status(
    request: Request,
    aggregator_id: str,
    status: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    actor = await get_current_web_user(request, db)
    if not actor:
        return RedirectResponse('/login', status_code=303)
    if staff_2fa_setup_required(actor):
        return _twofa_redirect()
    if actor.role not in [Role.superadmin.value, Role.admin.value]:
        return _aggregator_redirect('Not enough permissions')
    account = await _get_aggregator_for_update(db, aggregator_id)
    if not account:
        return _aggregator_redirect('Aggregator not found')
    status = status.strip().lower()
    if status not in {'active', 'blocked', 'archived'}:
        return _aggregator_redirect('Invalid aggregator status')
    account.status = status
    await audit(db, 'cabinet_aggregator_status_changed', 'aggregator', actor.id, account.id, client_ip(request), {'status': account.status})
    await db.commit()
    return _aggregator_redirect('Aggregator status changed', success=True)


@router.post('/cabinet/aggregators/{aggregator_id}/production/{action}')
async def cabinet_aggregator_production(request: Request, aggregator_id: UUID, action: str,
    confirmation: str = Form(''), reason: str = Form(''), db: AsyncSession = Depends(get_db)):
    from app.services.aggregator_credentials import change_aggregator_access
    from app.services.integration_modes import IntegrationModeError
    actor = await _require_superadmin_web(request, db)
    if isinstance(actor, RedirectResponse): return actor
    try:
        await change_aggregator_access(db, aggregator_id, actor, action=action, confirmation=confirmation,
            reason=reason, ip=client_ip(request))
    except IntegrationModeError as exc:
        await db.rollback()
        return _aggregator_redirect(str(exc)+' '+ ' '.join(r['message'] for r in exc.reasons))
    await db.commit()
    return _aggregator_redirect('Доступ агрегатора обновлён.', success=True)


@router.post('/cabinet/aggregators/{aggregator_id}/credentials/{action}')
async def cabinet_aggregator_credentials(request: Request, aggregator_id: UUID, action: str,
    mode: str = Form(''), confirmation: str = Form(''), reason: str = Form(''), key_id: UUID | None = Form(None),
    db: AsyncSession = Depends(get_db)):
    from app.services.aggregator_credentials import change_aggregator_key
    from app.services.integration_modes import IntegrationModeError
    actor = await _require_superadmin_web(request, db)
    if isinstance(actor, RedirectResponse): return actor
    try:
        key, secret = await change_aggregator_key(db, aggregator_id, actor, action=action, mode=mode,
            key_id=key_id, confirmation=confirmation, reason=reason, ip=client_ip(request))
    except IntegrationModeError as exc:
        await db.rollback()
        return _aggregator_redirect(str(exc))
    # Commit revocation/issue before making a secret available for consumption.
    await db.commit()
    if secret:
        account = await db.get(AggregatorAccount, aggregator_id)
        stored = await _store_aggregator_secret_flash(request, actor, {
            'name':account.name, 'api_key':key.api_key, 'secret_key':secret, 'mode':key.mode})
        if not stored: return _aggregator_redirect(AGGREGATOR_SECRET_FLASH_UNAVAILABLE_MESSAGE)
        realm = request_auth_realm(request) or 'staff'
        return RedirectResponse('/'+realm+'/cabinet/tradespace/secrets/aggregator',status_code=303)
    return _aggregator_redirect('Состояние ключа обновлено.', success=True)


@router.post('/cabinet/aggregators/{aggregator_id}/secret')
async def cabinet_regenerate_aggregator_secret(request: Request, aggregator_id: UUID, db: AsyncSession = Depends(get_db)):
    # Compatibility URL retains server guards; an old form cannot bypass the
    # explicit mode, key identity, reason and confirmation of the new command.
    form = await request.form()
    try: key_id = UUID(form['key_id']) if form.get('key_id') else None
    except ValueError: return _aggregator_redirect('Ключ не найден.')
    return await cabinet_aggregator_credentials(request, aggregator_id, 'rotate', mode=str(form.get('mode','')),
        confirmation=str(form.get('confirmation','')), reason=str(form.get('reason','')), key_id=key_id, db=db)


@router.post('/cabinet/requisites/create')
async def create_requisite(request: Request, method: str = Form(...), value: str = Form(...), bank_code: str = Form(""), operator_code: str = Form(""), bank_name: str = Form(""), full_name: str = Form(""), automation_id: str = Form(""), last4: str = Form(""), daily_limit: str = Form("500000"), operation_limit: int = Form(50), request_count: int = Form(10), timeframe: str = Form("час"), success_delay_minutes: int = Form(0), simultaneous_limit: int = Form(1), min_check: str = Form("100"), max_check: str = Form("150000"), status: str = Form("active"), trader_id: str | None = Form(None), db: AsyncSession = Depends(get_db)):
    user = await get_current_web_user(request, db)
    if not user:
        return RedirectResponse('/login', status_code=303)
    if staff_2fa_setup_required(user):
        return _twofa_redirect()
    if user.role not in [Role.operator.value, Role.trader.value, Role.superadmin.value, Role.admin.value]:
        return _redirect('permission_denied', 'requisites')
    target_trader_id = user.id if user.role in [Role.operator.value, Role.trader.value] else None
    if user.role in [Role.superadmin.value, Role.admin.value] and trader_id:
        target = (await db.execute(select(User).where(User.id == uuid.UUID(trader_id), User.role.in_([Role.operator.value, Role.trader.value])))).scalar_one_or_none()
        if not target:
            return _redirect('trader_not_found', 'requisites')
        target_trader_id = target.id
    if not target_trader_id:
        return _redirect('trader_required', 'requisites')
    normalized_method = normalize_payment_method(method, canonical_mobile=True)
    if normalized_method not in {PaymentMethod.sbp.value, PaymentMethod.c2c.value, PaymentMethod.mobile_commerce.value}:
        return _redirect('payment_method_invalid', 'requisites')
    provider = resolve_requisite_provider(normalized_method, bank_code=bank_code, operator_code=operator_code, bank_name=bank_name)
    if not provider:
        code = 'mobile_operator_required' if normalized_method == PaymentMethod.mobile_commerce.value else 'bank_required'
        return _redirect(code, 'requisites')
    req = Requisite(
        trader_id=target_trader_id,
        owner_name=full_name or user.email,
        full_name=full_name or None,
        method=normalized_method,
        value_encrypted=encrypt_secret(value.strip()),
        bank_code=provider.bank_code,
        operator_code=provider.operator_code,
        bank_name=provider.display_name,
        automation_id=automation_id or None,
        last4=(last4[-4:] if last4 else None),
        daily_limit=safe_decimal(daily_limit, '0'),
        operation_limit=operation_limit,
        request_count=max(1, int(request_count)),
        timeframe=(timeframe if timeframe in ["час", "день"] else "час"),
        success_delay_minutes=max(0, min(60, int(success_delay_minutes))),
        simultaneous_limit=max(1, min(50, int(simultaneous_limit))),
        min_check=max(Decimal('100.00'), safe_decimal(min_check, '100')),
        max_check=min(Decimal('150000.00'), safe_decimal(max_check, '150000')),
        status=status,
        enabled=status == 'active',
    )
    db.add(req)
    await audit(db, 'requisite_created', 'requisite', actor_id=user.id, ip=client_ip(request), details={"method": normalized_method, "provider_type": provider.provider_type, "provider_name": provider.display_name, "timeframe": timeframe, "success_delay_minutes": success_delay_minutes, "simultaneous_limit": simultaneous_limit})
    await db.commit()
    return _success_redirect('requisites')


@router.post('/cabinet/requisites/{requisite_id}/toggle')
async def toggle_requisite(request: Request, requisite_id: str, db: AsyncSession = Depends(get_db)):
    user = await get_current_web_user(request, db)
    if not user:
        return RedirectResponse('/login', status_code=303)
    if staff_2fa_setup_required(user):
        return _twofa_redirect()
    if user.role not in [Role.superadmin.value, Role.admin.value, Role.operator.value, Role.trader.value]:
        return _redirect('permission_denied', 'requisites')
    req = (await db.execute(select(Requisite).where(Requisite.id == uuid.UUID(requisite_id)))).scalar_one_or_none()
    if not req:
        return _redirect('requisite_not_found', 'requisites')
    if user.role in [Role.operator.value, Role.trader.value] and str(req.trader_id) != str(user.id):
        return _redirect('requisite_scope_denied', 'requisites')
    if req.status == 'deleted':
        return _redirect('requisite_deleted', 'requisites')
    req.enabled = not req.enabled
    req.status = 'active' if req.enabled else 'disabled'
    await audit(db, 'requisite_toggled', 'requisite', actor_id=user.id, target_id=str(req.id), ip=client_ip(request), details={'enabled': req.enabled})
    await db.commit()
    return _success_redirect('requisites')


@router.post('/cabinet/requisites/{requisite_id}/edit')
async def edit_requisite(
    request: Request,
    requisite_id: str,
    method: str = Form(...),
    value: str = Form(...),
    bank_code: str = Form(""),
    operator_code: str = Form(""),
    bank_name: str = Form(""),
    full_name: str = Form(""),
    automation_id: str = Form(""),
    last4: str = Form(""),
    daily_limit: str = Form("500000"),
    operation_limit: int = Form(50),
    request_count: int = Form(10),
    timeframe: str = Form("час"),
    success_delay_minutes: int = Form(0),
    simultaneous_limit: int = Form(1),
    min_check: str = Form("100"),
    max_check: str = Form("150000"),
    status: str = Form("active"),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_web_user(request, db)
    if not user:
        return RedirectResponse('/login', status_code=303)
    if staff_2fa_setup_required(user):
        return _twofa_redirect()
    req = (await db.execute(select(Requisite).where(Requisite.id == uuid.UUID(requisite_id)))).scalar_one_or_none()
    if not req:
        return _redirect('requisite_not_found', 'requisites')
    if not _can_manage_requisite(user, req):
        return _redirect('requisite_scope_denied', 'requisites')
    normalized_method = normalize_payment_method(method, canonical_mobile=True)
    if normalized_method not in {PaymentMethod.sbp.value, PaymentMethod.c2c.value, PaymentMethod.mobile_commerce.value}:
        return _redirect('payment_method_invalid', 'requisites')
    legacy_candidate = (bank_name or bank_code or operator_code or '').strip()
    provider = resolve_requisite_provider(
        normalized_method,
        bank_code=bank_code,
        operator_code=operator_code,
        bank_name=bank_name,
        allow_legacy=bool(legacy_candidate and legacy_candidate == (req.bank_name or '').strip()),
    )
    if not provider:
        code = 'mobile_operator_required' if normalized_method == PaymentMethod.mobile_commerce.value else 'bank_required'
        return _redirect(code, 'requisites')
    req.method = normalized_method
    req.value_encrypted = encrypt_secret(value.strip())
    req.owner_name = full_name.strip() or user.email
    req.full_name = full_name.strip() or None
    req.bank_code = provider.bank_code
    req.operator_code = provider.operator_code
    req.bank_name = provider.display_name
    req.automation_id = automation_id.strip() or None
    req.last4 = last4[-4:] if last4 else None
    req.daily_limit = safe_decimal(daily_limit, '0')
    req.operation_limit = max(1, int(operation_limit))
    req.request_count = max(1, int(request_count))
    req.timeframe = timeframe if timeframe in ["час", "день"] else "час"
    req.success_delay_minutes = max(0, min(60, int(success_delay_minutes)))
    req.simultaneous_limit = max(1, min(50, int(simultaneous_limit)))
    req.min_check = max(Decimal('1.00'), safe_decimal(min_check, '100'))
    req.max_check = min(Decimal('1000000.00'), safe_decimal(max_check, '150000'))
    req.status = status if status in ['active', 'disabled', 'review'] else 'active'
    req.enabled = req.status == 'active'
    await audit(db, 'requisite_updated', 'requisite', actor_id=user.id, target_id=str(req.id), ip=client_ip(request))
    await db.commit()
    return _success_redirect('requisites')


@router.post('/cabinet/requisites/{requisite_id}/delete')
async def delete_requisite(request: Request, requisite_id: str, db: AsyncSession = Depends(get_db)):
    user = await get_current_web_user(request, db)
    if not user:
        return RedirectResponse('/login', status_code=303)
    if user.role not in [Role.operator.value, Role.trader.value]:
        return _redirect('requisite_delete_denied', 'requisites')
    req = (await db.execute(select(Requisite).where(Requisite.id == uuid.UUID(requisite_id), Requisite.trader_id == user.id))).scalar_one_or_none()
    if not req:
        return _redirect('requisite_not_found', 'requisites')
    req.enabled = False
    req.status = 'deleted'
    await audit(db, 'requisite_deleted_by_trader', 'requisite', actor_id=user.id, target_id=str(req.id), ip=client_ip(request))
    await db.commit()
    return _success_redirect('requisites')


@router.post('/cabinet/traders/{trader_id}/block')
async def block_trader(request: Request, trader_id: str, db: AsyncSession = Depends(get_db)):
    actor = await get_current_web_user(request, db)
    if not actor:
        return RedirectResponse('/login', status_code=303)
    if staff_2fa_setup_required(actor):
        return _twofa_redirect()
    if actor.role != Role.superadmin.value:
        return _redirect('permission_denied', 'traders')
    trader = (await db.execute(select(User).where(User.id == uuid.UUID(trader_id), User.role.in_([Role.operator.value, Role.trader.value])))).scalar_one_or_none()
    if not trader:
        return _redirect('trader_not_found', 'traders')
    trader.is_locked = True
    await audit(db, 'trader_blocked', 'user', actor.id, trader.id, client_ip(request))
    await db.commit()
    return _success_redirect('traders')


@router.post('/cabinet/traders/{trader_id}/unblock')
async def unblock_trader(request: Request, trader_id: str, db: AsyncSession = Depends(get_db)):
    actor = await get_current_web_user(request, db)
    if not actor:
        return RedirectResponse('/login', status_code=303)
    if staff_2fa_setup_required(actor):
        return _twofa_redirect()
    if actor.role != Role.superadmin.value:
        return _redirect('permission_denied', 'traders')
    trader = (await db.execute(select(User).where(User.id == uuid.UUID(trader_id), User.role.in_([Role.operator.value, Role.trader.value])))).scalar_one_or_none()
    if not trader:
        return _redirect('trader_not_found', 'traders')
    trader.is_locked = False
    trader.failed_login_count = 0
    await audit(db, 'trader_unblocked', 'user', actor.id, trader.id, client_ip(request))
    await db.commit()
    return _success_redirect('traders')


@router.post('/cabinet/traders/{trader_id}/finance')
async def update_trader_finance(request: Request, trader_id: str, trader_balance: str = Form(...), trader_hold: str = Form(...), trader_traffic_priority: int = Form(...), trader_commission_percent: str = Form(""), assigned_merchants: list[str] = Form([]), db: AsyncSession = Depends(get_db)):
    actor = await get_current_web_user(request, db)
    if not actor:
        return RedirectResponse('/login', status_code=303)
    if staff_2fa_setup_required(actor):
        return _twofa_redirect()
    if actor.role != Role.superadmin.value:
        return _redirect('permission_denied', 'traders')
    trader = (await db.execute(select(User).where(User.id == uuid.UUID(trader_id), User.role.in_([Role.operator.value, Role.trader.value])))).scalar_one_or_none()
    if not trader:
        return _redirect('trader_not_found', 'traders')
    target_balance = max(Decimal('0.00'), safe_decimal(trader_balance, '0'))
    target_hold = min(target_balance, max(Decimal('0.00'), safe_decimal(trader_hold, '0')))
    trader.trader_traffic_priority = max(0, min(100, trader_traffic_priority))
    if trader_commission_percent.strip():
        trader.trader_commission_percent = _clamp_percent(trader_commission_percent, '7')
    trader.trader_assigned_merchants = assigned_merchants
    try:
        await manual_trader_adjustment(
            db,
            trader,
            target_balance=target_balance,
            target_hold=target_hold,
            operation_id=trader.id,
            idempotency_prefix=f'trader-manual-adjustment:{trader.id}:{datetime.now(timezone.utc).timestamp()}',
            reason='manual trader finance adjustment by superadmin',
        )
    except LedgerError as exc:
        await db.rollback()
        return _redirect('financial_invariant_error', 'traders')
    await audit(db, 'trader_finance_updated', 'user', actor.id, trader.id, client_ip(request), {'balance': str(trader.trader_balance), 'hold': str(trader.trader_hold), 'priority': trader.trader_traffic_priority, 'commission_percent': str(trader.trader_commission_percent), 'merchants': assigned_merchants})
    await db.commit()
    return _success_redirect('traders')


@router.post('/cabinet/security/password')
async def change_password(request: Request, current_password: str = Form(...), new_password: str = Form(...), db: AsyncSession = Depends(get_db)):
    user = await get_current_web_user(request, db)
    if not user:
        return RedirectResponse('/login', status_code=303)
    if not verify_password(current_password, user.password_hash):
        return _redirect('current_password_incorrect', 'security')
    if len(new_password) < 10:
        return _redirect('password_too_short', 'security')
    user.password_hash = hash_password(new_password)
    await revoke_refresh_tokens(db, user.id)
    await audit(db, 'password_changed', 'user', actor_id=user.id, target_id=str(user.id), ip=client_ip(request))
    await db.commit()
    _mark_session_event(request, 'password_changed')
    request.session.clear()
    return RedirectResponse('/login', status_code=303)


@router.post('/cabinet/security/2fa/prepare')
async def prepare_2fa(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_web_user(request, db)
    if not user:
        return RedirectResponse('/login', status_code=303)
    if not user.twofa_secret:
        user.twofa_secret = encrypt_secret(pyotp.random_base32())
        await db.commit()
    return _success_redirect('security')


@router.post('/cabinet/security/2fa/enable')
async def enable_2fa(request: Request, otp: str = Form(...), db: AsyncSession = Depends(get_db)):
    user = await get_current_web_user(request, db)
    if not user:
        return RedirectResponse('/login', status_code=303)
    if not user.twofa_secret:
        return _redirect('twofa_prepare_required', 'security')
    if not pyotp.TOTP(decrypt_secret(user.twofa_secret)).verify(otp, valid_window=1):
        return _redirect('twofa_invalid', 'security')
    user.twofa_enabled = True
    request.session['auth'] = _session_marker(user)
    request.session['twofa_verified'] = True
    await audit(db, '2fa_enabled', 'user', actor_id=user.id, target_id=str(user.id), ip=client_ip(request))
    await db.commit()
    return _success_redirect('security')


@router.post('/cabinet/security/2fa/disable')
async def disable_2fa(request: Request, otp: str = Form(''), db: AsyncSession = Depends(get_db)):
    user = await get_current_web_user(request, db)
    if not user:
        return RedirectResponse('/login', status_code=303)
    if user.role in STAFF_2FA_ROLES:
        return _redirect('twofa_disable_forbidden', 'security')
    if user.twofa_enabled and user.twofa_secret and not pyotp.TOTP(decrypt_secret(user.twofa_secret)).verify(otp, valid_window=1):
        return _redirect('twofa_invalid', 'security')
    user.twofa_enabled = False
    user.twofa_secret = None
    request.session.pop('twofa_verified', None)
    await audit(db, '2fa_disabled', 'user', actor_id=user.id, target_id=str(user.id), ip=client_ip(request))
    await db.commit()
    return _success_redirect('security')


async def can_confirm_deposit(db: AsyncSession, user: User, deposit: Deposit) -> bool:
    if user.role in [Role.superadmin.value, Role.admin.value]:
        return True
    if user.role not in [Role.operator.value, Role.trader.value]:
        return False
    req = (await db.execute(select(Requisite).where(Requisite.id == deposit.requisites_id))).scalar_one_or_none()
    return bool(req and str(req.trader_id) == str(user.id))


@router.post('/cabinet/deposits/{deposit_id}/confirm')
async def cabinet_confirm_deposit(deposit_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_web_user(request, db)
    if not user:
        return RedirectResponse('/login', status_code=303)
    if staff_2fa_setup_required(user):
        return _twofa_redirect()
    try:
        deposit_uuid = uuid.UUID(deposit_id)
    except ValueError:
        return _redirect('Заявка не найдена', 'deposits')
    dep = (await db.execute(select(Deposit).where(Deposit.id == deposit_uuid).with_for_update())).scalar_one_or_none()
    if not dep:
        return _redirect('Заявка не найдена', 'deposits')
    if not await can_confirm_deposit(db, user, dep):
        return _redirect('Нет доступа к подтверждению этой заявки', 'deposits')
    try:
        result = await confirm_deposit_payment(
            db,
            dep.id,
            actor_id=user.id,
            actor_ip=client_ip(request),
            audit_action='deposit_confirmed_from_cabinet',
            description='cabinet deposit confirmation',
        )
    except DepositConfirmationSystemError as exc:
        return _redirect('deposit_action_failed', 'deposits')
    except DepositConfirmationError as exc:
        return _redirect('deposit_action_failed', 'deposits')
    delivery = enqueue_deposit_confirmation_deliveries(result)
    if not result.confirmed:
        return _redirect(
            'Срок заявки истёк: выполнена финализация trader_timeout',
            'deposits',
        )
    if not delivery['webhook_enqueued']:
        return _redirect('webhook_queue_unavailable', 'deposits')
    return _success_redirect('deposits')


@router.post('/cabinet/deposits/{deposit_id}/decline')
async def cabinet_decline_deposit(
    deposit_id: str,
    request: Request,
    reason: str = Form(''),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_web_user(request, db)
    if not user:
        return RedirectResponse('/login', status_code=303)
    if staff_2fa_setup_required(user):
        return _twofa_redirect()
    if user.role not in [Role.superadmin.value, Role.admin.value]:
        raise HTTPException(403, 'Only admin or superadmin may decline a deposit')
    reason = reason.strip()
    if user.role == Role.superadmin.value and not reason:
        raise HTTPException(422, 'Decline reason is required')
    reason = reason or 'admin_decline'
    try:
        deposit_uuid = uuid.UUID(deposit_id)
    except ValueError:
        raise HTTPException(404, 'Deposit not found')
    dep = (await db.execute(
        select(Deposit).where(Deposit.id == deposit_uuid).with_for_update()
    )).scalar_one_or_none()
    if not dep:
        return _redirect('deposit_not_found', 'deposits')
    result = await finalize_unsuccessful_deposit(
        db,
        dep,
        reason=f'manual_decline:{reason[:400]}',
        target_status=DepositStatus.failed.value,
        actor_id=user.id,
        actor_role=user.role,
        actor_ip=client_ip(request),
        request_id=request.headers.get('X-Request-ID'),
        audit_action='deposit_declined_from_cabinet',
    )
    await db.commit()
    webhook_enqueued = bool(
        result.webhook_event_id
        and enqueue_webhook_delivery(result.webhook_event_id)
    )
    for callback_id in result.aggregator_callback_log_ids:
        enqueue_aggregator_callback_delivery(callback_id)
    if not result.changed:
        return _redirect('deposit_action_failed', 'deposits')
    if not webhook_enqueued:
        return _redirect('webhook_queue_unavailable', 'deposits')
    return _success_redirect('deposits')



@router.get('/cabinet/appeals/files/{filename}')
async def cabinet_appeal_file(filename: str, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_web_user(request, db)
    if not user:
        return RedirectResponse('/login', status_code=303)
    try:
        file_path = _safe_appeal_file_path(filename)
    except (ValueError, TypeError):
        raise HTTPException(404, 'file not found')
    if not file_path.exists():
        raise HTTPException(404, 'file not found')
    if not await _appeal_file_belongs_to_visible_appeal(db, user, filename):
        raise HTTPException(403, 'forbidden')
    return FileResponse(file_path)


@router.post('/cabinet/appeals/create')
async def cabinet_create_merchant_appeal(
    request: Request,
    operation_lookup: str = Form(...),
    amount: str = Form(...),
    requisite: str = Form(...),
    recipient_bank: str = Form(''),
    operation_date: str = Form(...),
    trader_id: str = Form(...),
    message: str = Form(''),
    receipt_file: UploadFile | None = File(None),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_web_user(request, db)
    if not user:
        return RedirectResponse('/login', status_code=303)
    if user.role != Role.merchant.value:
        return _redirect('Создавать апелляции может только merchant', 'appeals')
    merchant = (await db.execute(select(Merchant).where(Merchant.owner_id == user.id))).scalar_one_or_none()
    if not merchant:
        return _redirect('Merchant не найден', 'appeals')
    dep = await _merchant_deposit_by_lookup(db, merchant, operation_lookup, for_update=True)
    if not dep:
        return _redirect('Сделка не найдена', 'appeals')
    try:
        selected_trader_uuid = uuid.UUID(trader_id)
    except ValueError:
        return _redirect('Выберите трейдера', 'appeals')
    actual_trader_id = await _deposit_trader_id(db, dep)
    if actual_trader_id and actual_trader_id != str(selected_trader_uuid):
        return _redirect('Выберите трейдера, который привязан к этой сделке', 'appeals')
    duplicate = (await db.execute(
        select(Appeal).where(
            Appeal.operation_type == 'deposit',
            Appeal.operation_id == dep.id,
            Appeal.status.in_([AppealStatus.opened.value, AppealStatus.in_review.value]),
        )
    )).scalar_one_or_none()
    if duplicate:
        return _redirect('По этой сделке уже есть активная апелляция', 'appeals')
    try:
        receipt = await save_appeal_upload(receipt_file, required=True)
    except ValueError as exc:
        return _redirect(str(exc), 'appeals')
    claimed_amount = safe_decimal(amount, '0')
    if claimed_amount <= 0:
        return _redirect('Укажите корректную сумму', 'appeals')
    normalized_recipient_bank = normalize_bank_name(recipient_bank)
    if not normalized_recipient_bank:
        return _redirect('Choose a bank from the directory', 'appeals')
    previous_status = dep.status
    if dep.status != DepositStatus.paid.value:
        dep.status = DepositStatus.appeal_opened.value
    deadline = default_deadline()
    appeal = Appeal(
        operation_type='deposit',
        operation_id=dep.id,
        created_by=user.id,
        status=AppealStatus.opened.value,
        metadata_json={
            'merchant_id': str(merchant.id),
            'trader_id': str(selected_trader_uuid),
            'amount_claimed': str(claimed_amount),
            'requisite': requisite.strip()[:300],
            'recipient_bank': normalized_recipient_bank[:160],
            'operation_date': operation_date.strip()[:64],
            'receipt_file': receipt,
            'deadline_at': deadline.isoformat(),
            'previous_deposit_status': previous_status,
            'source': 'merchant_cabinet',
        },
    )
    db.add(appeal)
    await db.flush()
    db.add(AppealMessage(appeal_id=appeal.id, author_id=user.id, message=message.strip()[:2000] or 'Мерчант создал апелляцию.'))
    await audit(db, 'merchant_appeal_created', 'appeal', user.id, appeal.id, client_ip(request), {'deposit_id': str(dep.id), 'trader_id': str(selected_trader_uuid)})
    await db.commit()
    return _redirect('Апелляция создана', 'appeals')


@router.post('/cabinet/appeals/{appeal_id}/trader/accept')
async def cabinet_trader_accept_appeal(appeal_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_web_user(request, db)
    if not user:
        return RedirectResponse('/login', status_code=303)
    if user.role not in [Role.operator.value, Role.trader.value]:
        return _redirect('Недостаточно прав', 'appeals')
    try:
        appeal_uuid = uuid.UUID(appeal_id)
    except ValueError:
        return _redirect('Апелляция не найдена', 'appeals')
    appeal = (await db.execute(
        select(Appeal).where(Appeal.id == appeal_uuid)
    )).scalar_one_or_none()
    if not appeal or not await _appeal_visible_to_user(db, user, appeal):
        return _redirect('Апелляция не найдена', 'appeals')
    if appeal.status != AppealStatus.opened.value:
        return _redirect('Эта апелляция уже обработана', 'appeals')
    try:
        event = await approve_deposit_appeal(db, appeal, actor_id=user.id)
    except ValueError as exc:
        await db.rollback()
        return _redirect(str(exc), 'appeals')
    set_appeal_metadata(appeal, trader_decision='accepted', trader_decision_at=datetime.now(timezone.utc).isoformat())
    await audit(db, 'trader_appeal_accepted', 'appeal', user.id, appeal.id, client_ip(request))
    await db.commit()
    if event:
        enqueue_webhook_delivery(event.id)
    return _redirect('Апелляция подтверждена', 'appeals')


@router.post('/cabinet/appeals/{appeal_id}/trader/reject')
async def cabinet_trader_reject_appeal(
    appeal_id: str,
    request: Request,
    reason: str = Form(...),
    corrected_amount: str = Form(''),
    statement_file: UploadFile | None = File(None),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_web_user(request, db)
    if not user:
        return RedirectResponse('/login', status_code=303)
    if user.role not in [Role.operator.value, Role.trader.value]:
        return _redirect('Недостаточно прав', 'appeals')
    try:
        appeal_uuid = uuid.UUID(appeal_id)
    except ValueError:
        return _redirect('Апелляция не найдена', 'appeals')
    appeal = (await db.execute(
        select(Appeal).where(Appeal.id == appeal_uuid).with_for_update()
    )).scalar_one_or_none()
    if not appeal or not await _appeal_visible_to_user(db, user, appeal):
        return _redirect('Апелляция не найдена', 'appeals')
    if appeal.status != AppealStatus.opened.value:
        return _redirect('Эта апелляция уже передана на проверку', 'appeals')
    if reason not in TRADER_REJECTION_REASONS:
        return _redirect('Выберите корректную причину отклонения', 'appeals')
    meta_update = {
        'trader_decision': 'rejected',
        'trader_decision_at': datetime.now(timezone.utc).isoformat(),
        'trader_rejection_reason': reason,
        'trader_rejection_reason_label': TRADER_REJECTION_REASONS[reason],
    }
    if reason == 'wrong_amount':
        amount_value = safe_decimal(corrected_amount, '0')
        if amount_value <= 0:
            return _redirect('Укажите корректную сумму для причины неверная сумма', 'appeals')
        meta_update['corrected_amount'] = str(amount_value)
    if reason == 'no_payment':
        try:
            meta_update['statement_file'] = await save_appeal_upload(statement_file, required=True)
        except ValueError as exc:
            return _redirect('Для причины отсутствия платежа нужна выписка: ' + str(exc), 'appeals')
    set_appeal_metadata(appeal, **meta_update)
    appeal.status = AppealStatus.in_review.value
    appeal.decision = 'trader_rejected'
    db.add(AppealMessage(appeal_id=appeal.id, author_id=user.id, message='Трейдер отклонил апелляцию: ' + TRADER_REJECTION_REASONS[reason]))
    await audit(db, 'trader_appeal_rejected', 'appeal', user.id, appeal.id, client_ip(request), {'reason': reason})
    await db.commit()
    return _redirect('Апелляция отправлена на решение admin/superadmin', 'appeals')


@router.post('/cabinet/appeals/{appeal_id}/extend')
async def cabinet_extend_appeal(appeal_id: str, request: Request, minutes: int = Form(...), db: AsyncSession = Depends(get_db)):
    actor = await get_current_web_user(request, db)
    if not actor:
        return RedirectResponse('/login', status_code=303)
    if actor.role not in [Role.superadmin.value, Role.admin.value, Role.support.value]:
        return _redirect('Недостаточно прав', 'appeals')
    try:
        appeal_uuid = uuid.UUID(appeal_id)
    except ValueError:
        return _redirect('Апелляция не найдена', 'appeals')
    appeal = (await db.execute(select(Appeal).where(Appeal.id == appeal_uuid).with_for_update())).scalar_one_or_none()
    if not appeal:
        return _redirect('Апелляция не найдена', 'appeals')
    if appeal.status not in [AppealStatus.opened.value, AppealStatus.in_review.value]:
        return _redirect('Срок можно продлить только у активной апелляции', 'appeals')
    add_minutes = max(1, min(int(minutes), 24 * 60))
    base = max(appeal_deadline(appeal), datetime.now(timezone.utc))
    new_deadline = base + timedelta(minutes=add_minutes)
    set_appeal_metadata(appeal, deadline_at=new_deadline.isoformat(), extended_by=str(actor.id), extended_at=datetime.now(timezone.utc).isoformat())
    db.add(AppealMessage(appeal_id=appeal.id, author_id=actor.id, message=f'Срок обработки продлен на {add_minutes} мин.'))
    await audit(db, 'appeal_deadline_extended', 'appeal', actor.id, appeal.id, client_ip(request), {'minutes': add_minutes})
    await db.commit()
    return _redirect('Срок обработки продлен', 'appeals')


@router.post('/cabinet/appeals/{appeal_id}/staff/resolve')
async def cabinet_staff_resolve_appeal(appeal_id: str, request: Request, action: str = Form(...), db: AsyncSession = Depends(get_db)):
    actor = await get_current_web_user(request, db)
    if not actor:
        return RedirectResponse('/login', status_code=303)
    if actor.role not in [Role.superadmin.value, Role.admin.value]:
        return _redirect('Финальное решение может принять только admin/superadmin', 'appeals')
    try:
        appeal_uuid = uuid.UUID(appeal_id)
    except ValueError:
        return _redirect('Апелляция не найдена', 'appeals')
    appeal = (await db.execute(
        select(Appeal).where(Appeal.id == appeal_uuid)
    )).scalar_one_or_none()
    if not appeal:
        return _redirect('Апелляция не найдена', 'appeals')
    if appeal.status not in [AppealStatus.opened.value, AppealStatus.in_review.value]:
        return _redirect('Апелляция уже обработана', 'appeals')
    try:
        if action == 'approve':
            event = await approve_deposit_appeal(db, appeal, actor_id=actor.id)
            audit_action = 'staff_appeal_approved'
            message = 'Апелляция одобрена'
        elif action == 'reject':
            event = await reject_deposit_appeal(db, appeal, actor_id=actor.id)
            audit_action = 'staff_appeal_rejected'
            message = 'Апелляция отклонена'
        else:
            return _redirect('Неизвестное действие', 'appeals')
    except ValueError as exc:
        await db.rollback()
        return _redirect(str(exc), 'appeals')
    await audit(db, audit_action, 'appeal', actor.id, appeal.id, client_ip(request))
    await db.commit()
    if event:
        enqueue_webhook_delivery(event.id)
    return _redirect(message, 'appeals')



@router.get('/cabinet/rates/usdt-rub')
async def cabinet_usdt_rub_rate(request: Request, db: AsyncSession = Depends(get_db)):
    if await _session_identity_changed(request):
        _mark_session_event(request, SESSION_IDENTITY_CHANGED_CODE)
        return JSONResponse(
            {
                'code': SESSION_IDENTITY_CHANGED_CODE,
                'detail': SESSION_IDENTITY_CHANGED_MESSAGE,
            },
            status_code=409,
            headers={'Cache-Control': 'no-store, max-age=0'},
        )
    user = await get_current_web_user(request, db)
    if not user:
        _mark_session_event(request, 'polling_session_missing_or_expired')
        return JSONResponse({'detail': 'not authenticated'}, status_code=401)
    if staff_2fa_setup_required(user):
        _mark_session_event(request, 'polling_2fa_required')
        return JSONResponse({'detail': '2fa required'}, status_code=403)
    if user.role == Role.merchant.value:
        merchant = (await db.execute(select(Merchant).where(Merchant.owner_id == user.id, Merchant.is_archived.is_(False)))).scalar_one_or_none()
        if merchant:
            try:
                quote = await merchant_settlement_quote(db, merchant.id)
            except MerchantSettlementRateUnavailable as exc:
                return JSONResponse(
                    {'detail': exc.code, 'code': exc.code},
                    status_code=503,
                )
            return {
                'rate_rub': str(quote['rate_rub']),
                'rate_source': quote['rate_source'],
                'source': quote['rate_source'],
                'updated_at': quote.get('rate_updated_at').isoformat() if quote.get('rate_updated_at') else None,
                'stale': bool(quote.get('rate_stale')),
                'fee_usdt': str(quote['fee_usdt']),
                'fee_rub': str(quote['fee_rub']),
                'available_rub': str(quote['available_rub']),
                'pending_rub': str(quote['pending_rub']),
                'max_request_usdt': str(quote['max_request_usdt']),
                'cache_seconds': settings.RAPIRA_RATES_CACHE_SECONDS,
            }
    rate_quote = await get_rapira_rub_usdt_quote()
    rate_source = f'{rate_quote.source} stale' if rate_quote.stale else rate_quote.source
    return {
        'rate_rub': str(rate_quote.rate_rub),
        'rate_source': rate_source,
        'source': rate_source,
        'updated_at': rate_quote.updated_at.isoformat(),
        'stale': rate_quote.stale,
        'cache_seconds': settings.RAPIRA_RATES_CACHE_SECONDS,
    }

@router.post('/cabinet/settlements/request')
async def request_merchant_settlement(
    request: Request,
    amount_usdt: str = Form(...),
    trc20_address: str = Form(...),
    idempotency_key: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_web_user(request, db)
    if not user:
        return RedirectResponse('/login', status_code=303)
    if user.role != Role.merchant.value:
        return _redirect('permission_denied', 'payouts')
    merchant = (await db.execute(select(Merchant).where(Merchant.owner_id == user.id))).scalar_one_or_none()
    if not merchant:
        return _redirect('merchant_not_found', 'payouts')
    try:
        await create_merchant_settlement(
            db,
            merchant_id=merchant.id,
            requested_by_id=user.id,
            amount_usdt=safe_decimal(amount_usdt, '0'),
            trc20_address=trc20_address,
            idempotency_key=idempotency_key,
            actor_ip=client_ip(request),
        )
    except MerchantSettlementRateUnavailable as exc:
        await db.rollback()
        return JSONResponse(
            {'detail': exc.code, 'code': exc.code},
            status_code=503,
        )
    except MerchantSettlementError as exc:
        await db.rollback()
        return _redirect(exc.code, 'payouts')
    await db.commit()
    return _success_redirect('payouts')


@router.post('/cabinet/settlements/{settlement_id}/approve')
async def approve_merchant_settlement(
    request: Request,
    settlement_id: str,
    tx_hash: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    actor = await get_current_web_user(request, db)
    if not actor:
        return RedirectResponse('/login', status_code=303)
    if staff_2fa_setup_required(actor):
        return _twofa_redirect()
    if actor.role != Role.superadmin.value:
        return _redirect('permission_denied', 'payouts')
    try:
        settlement_uuid = uuid.UUID(settlement_id)
    except ValueError:
        return _redirect('action_failed', 'payouts')
    try:
        await complete_merchant_settlement(
            db,
            settlement_id=settlement_uuid,
            actor_id=actor.id,
            tx_hash=tx_hash,
            actor_ip=client_ip(request),
        )
    except MerchantSettlementError as exc:
        await db.rollback()
        return _redirect(exc.code, 'payouts')
    await db.commit()
    return _success_redirect('payouts')


@router.post('/cabinet/settlements/{settlement_id}/reject')
async def reject_merchant_settlement(request: Request, settlement_id: str, reject_reason: str = Form(''), db: AsyncSession = Depends(get_db)):
    actor = await get_current_web_user(request, db)
    if not actor:
        return RedirectResponse('/login', status_code=303)
    if staff_2fa_setup_required(actor):
        return _twofa_redirect()
    if actor.role != Role.superadmin.value:
        return _redirect('permission_denied', 'payouts')
    try:
        settlement_uuid = uuid.UUID(settlement_id)
    except ValueError:
        return _redirect('action_failed', 'payouts')
    try:
        await reject_merchant_settlement_service(
            db,
            settlement_id=settlement_uuid,
            actor_id=actor.id,
            reason=reject_reason,
            actor_ip=client_ip(request),
        )
    except MerchantSettlementError as exc:
        await db.rollback()
        return _redirect(exc.code, 'payouts')
    await db.commit()
    return _success_redirect('payouts')
def _form_bool(value) -> bool:
    return str(value).lower() in {'1', 'true', 'on', 'yes', 'да'}


async def _require_superadmin_web(request: Request, db: AsyncSession) -> User | RedirectResponse:
    actor = await get_current_web_user(request, db)
    if not actor:
        return RedirectResponse('/login', status_code=303)
    if actor.role != Role.superadmin.value:
        return _redirect('Недостаточно прав', 'antiscam')
    if staff_2fa_setup_required(actor):
        return _twofa_redirect()
    if not request.scope.get('session', {}).get('twofa_verified'):
        request.scope.get('session', {}).clear()
        return RedirectResponse(
            '/login?error='
            + quote_plus('Требуется новый вход с 2FA'),
            status_code=303,
        )
    return actor


async def _require_platform_wallet_reader(
    request: Request,
    db: AsyncSession,
) -> User:
    actor = await get_current_web_user(request, db)
    if not actor:
        raise HTTPException(401, 'authentication_required')
    if actor.role not in {
        Role.superadmin.value,
        Role.admin.value,
        Role.operator.value,
        Role.trader.value,
    }:
        raise HTTPException(403, 'platform_wallet_access_denied')
    if actor.role in STAFF_2FA_ROLES:
        if staff_2fa_setup_required(actor):
            raise HTTPException(403, '2FA setup required')
        if not request.scope.get('session', {}).get('twofa_verified'):
            raise HTTPException(403, 'fresh 2FA session required')
    return actor


@router.get('/cabinet/platform-wallet/qr')
async def cabinet_platform_wallet_qr(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    await _require_platform_wallet_reader(request, db)
    wallet = await get_active_platform_wallet(db)
    if wallet is None:
        raise HTTPException(404, 'platform_wallet_not_configured')
    etag = f'"platform-wallet-v{wallet.version}"'
    headers = {
        'Cache-Control': 'private, max-age=300, must-revalidate',
        'ETag': etag,
        'X-Platform-Wallet-Version': str(wallet.version),
        'X-Content-Type-Options': 'nosniff',
    }
    if request.headers.get('if-none-match') == etag:
        return Response(status_code=304, headers=headers)
    return Response(
        content=platform_wallet_qr_png(wallet),
        media_type='image/png',
        headers=headers,
    )


@router.post('/cabinet/platform-wallet')
async def cabinet_platform_wallet_update(
    request: Request,
    address: str = Form(...),
    label: str = Form(''),
    change_reason: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    current_user = await get_current_web_user(request, db)
    if current_user and current_user.role != Role.superadmin.value:
        raise HTTPException(403, 'platform_wallet_manage_forbidden')
    actor = await _require_superadmin_web(request, db)
    if isinstance(actor, RedirectResponse):
        return actor
    try:
        change = await set_active_platform_wallet(
            db,
            address=address,
            label=label,
            actor_id=actor.id,
            change_reason=change_reason,
        )
        await audit(
            db,
            (
                'platform_wallet_replaced'
                if change.changed
                else 'platform_wallet_submit_idempotent'
            ),
            'platform_crypto_wallet',
            actor.id,
            change.wallet.id,
            client_ip(request),
            {
                'asset': change.wallet.asset,
                'network': change.wallet.network,
                'version': change.wallet.version,
                'previous_wallet_id': (
                    str(change.previous_wallet_id)
                    if change.previous_wallet_id
                    else None
                ),
                'address_suffix': change.wallet.address[-6:],
                'label_configured': bool(change.wallet.label),
                'reason': change_reason.strip()[:500],
            },
        )
        await db.commit()
    except PlatformWalletError as exc:
        await db.rollback()
        return _redirect(str(exc), 'wallet')
    except IntegrityError:
        await db.rollback()
        return _redirect('platform_wallet_concurrent_update', 'wallet')
    return _success_redirect('wallet')


@router.post('/cabinet/ai-office/config')
async def cabinet_ai_office_save_config(
    request: Request,
    enabled: bool = Form(False),
    environment: str = Form('local'),
    base_url: str = Form(''),
    health_path: str = Form('/api/health'),
    api_version: str = Form(''),
    auth_type: str = Form('none'),
    api_key: str = Form(''),
    bearer_token: str = Form(''),
    hmac_secret: str = Form(''),
    timeout_seconds: str = Form('5'),
    connect_timeout_seconds: str = Form('2'),
    max_retries: int = Form(0),
    verify_tls: bool = Form(False),
    selected_events: list[str] = Form([]),
    change_reason: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    actor = await _require_superadmin_web(request, db)
    if isinstance(actor, RedirectResponse):
        return actor
    reason = change_reason.strip()
    if not reason:
        return _redirect(
            'ai_office_change_reason_required',
            'ai-office',
        )
    try:
        config = await save_ai_integration_config(
            db,
            actor_id=actor.id,
            enabled=enabled,
            environment=environment,
            base_url=base_url,
            health_path=health_path,
            api_version=api_version,
            auth_type=auth_type,
            api_key=api_key,
            bearer_token=bearer_token,
            hmac_secret=hmac_secret,
            timeout_seconds=timeout_seconds,
            connect_timeout_seconds=connect_timeout_seconds,
            max_retries=max_retries,
            verify_tls=verify_tls,
            selected_events=selected_events,
        )
        await audit(
            db,
            'ai_office_config_updated',
            'ai_integration_config',
            actor.id,
            config.id,
            client_ip(request),
            {
                'provider': config.provider,
                'enabled': config.enabled,
                'environment': config.environment,
                'auth_type': config.auth_type,
                'verify_tls': config.verify_tls,
                'selected_events': list(config.selected_events or []),
                'api_key_updated': bool(api_key.strip()),
                'bearer_token_updated': bool(bearer_token.strip()),
                'hmac_secret_updated': bool(hmac_secret.strip()),
                'reason': reason[:500],
            },
        )
        await db.commit()
    except (AIIntegrationError, ValueError) as exc:
        await db.rollback()
        return _redirect(str(exc), 'ai-office')
    return _success_redirect('ai-office')


@router.post('/cabinet/ai-office/config/secrets/{secret_kind}/clear')
async def cabinet_ai_office_clear_secret(
    secret_kind: str,
    request: Request,
    change_reason: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    actor = await _require_superadmin_web(request, db)
    if isinstance(actor, RedirectResponse):
        return actor
    reason = change_reason.strip()
    if not reason:
        return _redirect(
            'ai_office_change_reason_required',
            'ai-office',
        )
    try:
        config = await clear_ai_integration_secret(
            db,
            actor_id=actor.id,
            secret_kind=secret_kind,
        )
        await audit(
            db,
            'ai_office_secret_cleared',
            'ai_integration_config',
            actor.id,
            config.id,
            client_ip(request),
            {
                'provider': config.provider,
                'secret_kind': secret_kind,
                'reason': reason[:500],
            },
        )
        await db.commit()
    except AIIntegrationError as exc:
        await db.rollback()
        return _redirect(str(exc), 'ai-office')
    return _success_redirect('ai-office')


@router.post('/cabinet/ai-office/test-connection')
async def cabinet_ai_office_test_connection(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    actor = await _require_superadmin_web(request, db)
    if isinstance(actor, RedirectResponse):
        return actor
    config = await get_ai_integration_config(db)
    if config is None:
        return _redirect('ai_office_config_not_found', 'ai-office')
    actor_id = actor.id
    config_id = config.id
    try:
        snapshot = ai_connection_snapshot(config)
        result = await run_ai_connection_test_without_transaction(
            db,
            snapshot,
        )
    except AIIntegrationError as exc:
        await db.rollback()
        return _redirect(str(exc), 'ai-office')

    config = (
        await db.execute(
            select(AIIntegrationConfig)
            .where(AIIntegrationConfig.id == config_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if config is None:
        await db.rollback()
        return _redirect('ai_office_config_not_found', 'ai-office')
    record_ai_connection_result(config, result)
    config.updated_by = actor_id
    await audit(
        db,
        'ai_office_connection_tested',
        'ai_integration_config',
        actor_id,
        config.id,
        client_ip(request),
        {
            'provider': config.provider,
            'environment': config.environment,
            'status': result.status,
            'latency_ms': result.latency_ms,
            'error_code': result.error_code,
        },
    )
    await db.commit()
    if result.success:
        return _success_redirect('ai-office')
    return _redirect('ai_office_connection_failed', 'ai-office')


@router.post('/cabinet/antiscam/settings')
async def cabinet_antiscam_save_settings(
    request: Request,
    antiscam_enabled: bool = Form(False),
    auto_disable_traffic_enabled: bool = Form(False),
    failed_payments_in_row_limit_requisite: int = Form(5),
    failed_payments_in_row_limit_trader: int = Form(10),
    min_payments_for_conversion_check: int = Form(20),
    conversion_check_window_minutes: int = Form(60),
    min_requisite_conversion_percent: str = Form('35'),
    min_trader_conversion_percent: str = Form('45'),
    conversion_drop_percent_limit: str = Form('40'),
    max_confirmation_delay_minutes: int = Form(10),
    merchant_complaints_limit_requisite: int = Form(3),
    merchant_complaints_limit_trader: int = Form(5),
    high_amount_extra_risk_enabled: bool = Form(False),
    high_amount_threshold: str = Form('100000'),
    freeze_withdrawals_on_trader_auto_pause: bool = Form(False),
    default_reinstate_mode: str = Form('limited'),
    limited_reinstate_duration_minutes: int = Form(120),
    limited_reinstate_max_active_payments: int = Form(3),
    limited_reinstate_max_amount: str = Form('30000'),
    db: AsyncSession = Depends(get_db),
):
    actor = await _require_superadmin_web(request, db)
    if isinstance(actor, RedirectResponse):
        return actor
    settings_row = await get_global_settings(db, for_update=True)
    before = {'antiscam_enabled': settings_row.antiscam_enabled, 'auto_disable_traffic_enabled': settings_row.auto_disable_traffic_enabled}
    settings_row.antiscam_enabled = bool(antiscam_enabled)
    settings_row.auto_disable_traffic_enabled = bool(auto_disable_traffic_enabled)
    settings_row.failed_payments_in_row_limit_requisite = max(1, int(failed_payments_in_row_limit_requisite))
    settings_row.failed_payments_in_row_limit_trader = max(1, int(failed_payments_in_row_limit_trader))
    settings_row.min_payments_for_conversion_check = max(1, int(min_payments_for_conversion_check))
    settings_row.conversion_check_window_minutes = max(5, int(conversion_check_window_minutes))
    settings_row.min_requisite_conversion_percent = safe_decimal(min_requisite_conversion_percent, '35')
    settings_row.min_trader_conversion_percent = safe_decimal(min_trader_conversion_percent, '45')
    settings_row.conversion_drop_percent_limit = safe_decimal(conversion_drop_percent_limit, '40')
    settings_row.max_confirmation_delay_minutes = max(1, int(max_confirmation_delay_minutes))
    settings_row.merchant_complaints_limit_requisite = max(1, int(merchant_complaints_limit_requisite))
    settings_row.merchant_complaints_limit_trader = max(1, int(merchant_complaints_limit_trader))
    settings_row.high_amount_extra_risk_enabled = bool(high_amount_extra_risk_enabled)
    settings_row.high_amount_threshold = safe_decimal(high_amount_threshold, '100000')
    settings_row.freeze_withdrawals_on_trader_auto_pause = bool(freeze_withdrawals_on_trader_auto_pause)
    settings_row.default_reinstate_mode = default_reinstate_mode if default_reinstate_mode in {'full', 'limited', 'low_amount_only', 'no_high_amount'} else 'limited'
    settings_row.limited_reinstate_duration_minutes = max(5, int(limited_reinstate_duration_minutes))
    settings_row.limited_reinstate_max_active_payments = max(1, int(limited_reinstate_max_active_payments))
    settings_row.limited_reinstate_max_amount = safe_decimal(limited_reinstate_max_amount, '30000')
    await audit(db, 'superadmin_changed_global_antiscam_settings', 'antiscam_settings', actor.id, settings_row.id, client_ip(request), {'before': before})
    await db.commit()
    return _redirect('Настройки антискама сохранены', 'antiscam')


@router.post('/cabinet/antiscam/traders/{trader_id}/settings')
async def cabinet_antiscam_save_trader_settings(
    request: Request,
    trader_id: str,
    use_global_antiscam_settings: bool = Form(False),
    antiscam_enabled: bool = Form(False),
    failed_payments_in_row_limit: int = Form(10),
    min_conversion_percent: str = Form('45'),
    conversion_check_window_minutes: int = Form(60),
    conversion_drop_percent_limit: str = Form('40'),
    max_confirmation_delay_minutes: int = Form(10),
    max_active_payments_when_risky: int = Form(3),
    allow_high_amount_traffic: bool = Form(False),
    risk_level: str = Form('strict'),
    auto_disable_requisites_enabled: bool = Form(False),
    auto_disable_trader_enabled: bool = Form(False),
    freeze_withdrawals_on_auto_pause: bool = Form(False),
    db: AsyncSession = Depends(get_db),
):
    actor = await _require_superadmin_web(request, db)
    if isinstance(actor, RedirectResponse):
        return actor
    try:
        trader_uuid = uuid.UUID(trader_id)
    except ValueError:
        return _redirect('Трейдер не найден', 'antiscam')
    trader = (await db.execute(select(User).where(User.id == trader_uuid, User.role.in_([Role.operator.value, Role.trader.value])).with_for_update())).scalar_one_or_none()
    if not trader:
        return _redirect('Трейдер не найден', 'antiscam')
    settings_row = await get_or_create_trader_settings(db, trader.id)
    settings_row.use_global_antiscam_settings = bool(use_global_antiscam_settings)
    settings_row.antiscam_enabled = bool(antiscam_enabled)
    settings_row.failed_payments_in_row_limit = max(1, int(failed_payments_in_row_limit))
    settings_row.min_conversion_percent = safe_decimal(min_conversion_percent, '45')
    settings_row.conversion_check_window_minutes = max(5, int(conversion_check_window_minutes))
    settings_row.conversion_drop_percent_limit = safe_decimal(conversion_drop_percent_limit, '40')
    settings_row.max_confirmation_delay_minutes = max(1, int(max_confirmation_delay_minutes))
    settings_row.max_active_payments_when_risky = max(1, int(max_active_payments_when_risky))
    settings_row.allow_high_amount_traffic = bool(allow_high_amount_traffic)
    settings_row.risk_level = risk_level if risk_level in {'strict', 'normal', 'trusted'} else 'strict'
    settings_row.auto_disable_requisites_enabled = bool(auto_disable_requisites_enabled)
    settings_row.auto_disable_trader_enabled = bool(auto_disable_trader_enabled)
    settings_row.freeze_withdrawals_on_auto_pause = bool(freeze_withdrawals_on_auto_pause)
    await audit(db, 'superadmin_changed_trader_antiscam_settings', 'user', actor.id, trader.id, client_ip(request), {'settings_id': str(settings_row.id)})
    await db.commit()
    return _redirect('Настройки трейдера сохранены', 'antiscam')


@router.post('/cabinet/antiscam/requisites/{requisite_id}/settings')
async def cabinet_antiscam_save_requisite_settings(
    request: Request,
    requisite_id: str,
    antiscam_enabled: bool = Form(False),
    failed_payments_in_row_limit: int = Form(5),
    min_conversion_percent: str = Form('35'),
    conversion_check_window_minutes: int = Form(60),
    max_confirmation_delay_minutes: int = Form(10),
    allow_high_amount_traffic: bool = Form(False),
    limited_max_active_payments: int = Form(3),
    limited_max_amount: str = Form('30000'),
    db: AsyncSession = Depends(get_db),
):
    actor = await _require_superadmin_web(request, db)
    if isinstance(actor, RedirectResponse):
        return actor
    try:
        req_uuid = uuid.UUID(requisite_id)
    except ValueError:
        return _redirect('Реквизит не найден', 'antiscam')
    req = (await db.execute(select(Requisite).where(Requisite.id == req_uuid).with_for_update())).scalar_one_or_none()
    if not req:
        return _redirect('Реквизит не найден', 'antiscam')
    settings_row = await get_or_create_requisite_settings(db, req.id)
    settings_row.antiscam_enabled = bool(antiscam_enabled)
    settings_row.failed_payments_in_row_limit = max(1, int(failed_payments_in_row_limit))
    settings_row.min_conversion_percent = safe_decimal(min_conversion_percent, '35')
    settings_row.conversion_check_window_minutes = max(5, int(conversion_check_window_minutes))
    settings_row.max_confirmation_delay_minutes = max(1, int(max_confirmation_delay_minutes))
    settings_row.allow_high_amount_traffic = bool(allow_high_amount_traffic)
    settings_row.limited_max_active_payments = max(1, int(limited_max_active_payments))
    settings_row.limited_max_amount = safe_decimal(limited_max_amount, '30000')
    await audit(db, 'superadmin_changed_requisite_antiscam_settings', 'requisite', actor.id, req.id, client_ip(request), {'settings_id': str(settings_row.id)})
    await db.commit()
    return _redirect('Настройки реквизита сохранены', 'antiscam')


@router.post('/cabinet/antiscam/traders/{trader_id}/pause')
async def cabinet_antiscam_pause_trader(request: Request, trader_id: str, decision_reason: str = Form('Ручная пауза superadmin'), db: AsyncSession = Depends(get_db)):
    actor = await _require_superadmin_web(request, db)
    if isinstance(actor, RedirectResponse):
        return actor
    try:
        trader_uuid = uuid.UUID(trader_id)
    except ValueError:
        return _redirect('Трейдер не найден', 'antiscam')
    trader = (await db.execute(select(User).where(User.id == trader_uuid).with_for_update())).scalar_one_or_none()
    if not trader:
        return _redirect('Трейдер не найден', 'antiscam')
    old = trader.trader_traffic_status
    trader.trader_traffic_status = TrafficStatus.manual_paused.value
    await audit(db, 'manual_superadmin_pause_trader', 'user', actor.id, trader.id, client_ip(request), {'before_status': old, 'after_status': trader.trader_traffic_status, 'reason': decision_reason})
    await db.commit()
    return _redirect('Трейдер поставлен на паузу', 'antiscam')


@router.post('/cabinet/antiscam/requisites/{requisite_id}/pause')
async def cabinet_antiscam_pause_requisite(request: Request, requisite_id: str, decision_reason: str = Form('Ручная пауза superadmin'), db: AsyncSession = Depends(get_db)):
    actor = await _require_superadmin_web(request, db)
    if isinstance(actor, RedirectResponse):
        return actor
    try:
        req_uuid = uuid.UUID(requisite_id)
    except ValueError:
        return _redirect('Реквизит не найден', 'antiscam')
    req = (await db.execute(select(Requisite).where(Requisite.id == req_uuid).with_for_update())).scalar_one_or_none()
    if not req:
        return _redirect('Реквизит не найден', 'antiscam')
    old = req.traffic_status
    req.traffic_status = TrafficStatus.manual_paused.value
    req.enabled = False
    await audit(db, 'manual_superadmin_pause_requisite', 'requisite', actor.id, req.id, client_ip(request), {'before_status': old, 'after_status': req.traffic_status, 'reason': decision_reason})
    await db.commit()
    return _redirect('Реквизит поставлен на паузу', 'antiscam')


@router.post('/cabinet/antiscam/traders/{trader_id}/reinstate')
async def cabinet_antiscam_reinstate_trader(request: Request, trader_id: str, decision_reason: str = Form(...), proofs_checked: bool = Form(False), proof_reference: str = Form(''), reinstate_mode: str = Form('limited'), db: AsyncSession = Depends(get_db)):
    actor = await _require_superadmin_web(request, db)
    if isinstance(actor, RedirectResponse):
        return actor
    try:
        trader_uuid = uuid.UUID(trader_id)
    except ValueError:
        return _redirect('Трейдер не найден', 'antiscam')
    trader = (await db.execute(select(User).where(User.id == trader_uuid).with_for_update())).scalar_one_or_none()
    if not trader:
        return _redirect('Трейдер не найден', 'antiscam')
    try:
        await reinstate_trader(db, trader=trader, actor=actor, decision_reason=decision_reason, proofs_checked=proofs_checked, proof_reference=proof_reference, reinstate_mode=reinstate_mode)
    except ValueError as exc:
        await db.rollback()
        return _redirect(str(exc), 'antiscam')
    await db.commit()
    return _redirect('Решение по трейдеру сохранено', 'antiscam')


@router.post('/cabinet/antiscam/requisites/{requisite_id}/reinstate')
async def cabinet_antiscam_reinstate_requisite(request: Request, requisite_id: str, decision_reason: str = Form(...), proofs_checked: bool = Form(False), proof_reference: str = Form(''), reinstate_mode: str = Form('limited'), db: AsyncSession = Depends(get_db)):
    actor = await _require_superadmin_web(request, db)
    if isinstance(actor, RedirectResponse):
        return actor
    try:
        req_uuid = uuid.UUID(requisite_id)
    except ValueError:
        return _redirect('Реквизит не найден', 'antiscam')
    req = (await db.execute(select(Requisite).where(Requisite.id == req_uuid).with_for_update())).scalar_one_or_none()
    if not req:
        return _redirect('Реквизит не найден', 'antiscam')
    try:
        await reinstate_requisite(db, req=req, actor=actor, decision_reason=decision_reason, proofs_checked=proofs_checked, proof_reference=proof_reference, reinstate_mode=reinstate_mode)
    except ValueError as exc:
        await db.rollback()
        return _redirect(str(exc), 'antiscam')
    await db.commit()
    return _redirect('Решение по реквизиту сохранено', 'antiscam')


@router.post('/cabinet/antiscam/traders/{trader_id}/withdrawals')
async def cabinet_antiscam_trader_withdrawals(request: Request, trader_id: str, frozen: bool = Form(False), db: AsyncSession = Depends(get_db)):
    actor = await _require_superadmin_web(request, db)
    if isinstance(actor, RedirectResponse):
        return actor
    try:
        trader_uuid = uuid.UUID(trader_id)
    except ValueError:
        return _redirect('Трейдер не найден', 'antiscam')
    trader = (await db.execute(select(User).where(User.id == trader_uuid).with_for_update())).scalar_one_or_none()
    if not trader:
        return _redirect('Трейдер не найден', 'antiscam')
    old = trader.trader_withdrawals_frozen
    trader.trader_withdrawals_frozen = bool(frozen)
    await audit(db, 'superadmin_froze_withdrawals' if trader.trader_withdrawals_frozen else 'superadmin_unfroze_withdrawals', 'user', actor.id, trader.id, client_ip(request), {'before': old, 'after': trader.trader_withdrawals_frozen})
    await db.commit()
    return _redirect('Настройка вывода трейдера сохранена', 'antiscam')


@router.post('/cabinet/rolling/merchants/{merchant_id}/transfers')
async def cabinet_rolling_register_transfer(
    merchant_id: str,
    request: Request,
    amount_usdt: str = Form(...),
    network: str = Form(...),
    destination_address: str = Form(...),
    tx_hash: str = Form(...),
    sent_at: str = Form(...),
    comment: str = Form(''),
    idempotency_key: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    actor = await _require_superadmin_web(request, db)
    if isinstance(actor, RedirectResponse):
        return actor
    try:
        merchant_uuid = uuid.UUID(merchant_id)
        amount = Decimal(amount_usdt)
        sent = datetime.fromisoformat(sent_at.replace('Z', '+00:00'))
    except (ValueError, InvalidOperation):
        raise HTTPException(422, 'Invalid merchant, amount, or sent_at')
    kwargs = {
        'merchant_id': merchant_uuid,
        'actor_id': actor.id,
        'amount_usdt': amount,
        'network': network,
        'destination_address': destination_address,
        'tx_hash': tx_hash,
        'sent_at': sent,
        'comment': comment,
        'idempotency_key': idempotency_key,
    }
    try:
        transfer = await register_rolling_transfer(db, **kwargs)
    except IntegrityError:
        await db.rollback()
        try:
            transfer = await register_rolling_transfer(db, **kwargs)
        except RollingError as exc:
            await db.rollback()
            return _redirect(str(exc), 'rolling')
        except IntegrityError:
            await db.rollback()
            return _redirect(
                'Transfer с такими network и tx hash уже зарегистрирован',
                'rolling',
            )
    except RollingError as exc:
        await db.rollback()
        return _redirect(str(exc), 'rolling')
    await db.commit()
    return _success_redirect('rolling')


async def _merchant_for_rolling_action(
    request: Request,
    db: AsyncSession,
) -> tuple[User, Merchant] | RedirectResponse:
    user = await get_current_web_user(request, db)
    if not user:
        return RedirectResponse('/login', status_code=303)
    if user.role != Role.merchant.value:
        return _redirect('Действие доступно только мерчанту', 'Пополнения')
    if user.twofa_enabled and not request.scope.get('session', {}).get(
        'twofa_verified'
    ):
        return RedirectResponse('/merchant/login?error=2fa_required', status_code=303)
    merchant = (
        await db.execute(
            select(Merchant).where(
                Merchant.owner_id == user.id,
                Merchant.is_archived.is_(False),
            )
        )
    ).scalar_one_or_none()
    if not merchant:
        return _redirect('Merchant не найден', 'Пополнения')
    return user, merchant


@router.post('/cabinet/rolling/transfers/{transfer_id}/confirm')
async def cabinet_rolling_confirm_transfer(
    transfer_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    identity = await _merchant_for_rolling_action(request, db)
    if isinstance(identity, RedirectResponse):
        return identity
    user, merchant = identity
    try:
        transfer_uuid = uuid.UUID(transfer_id)
        transfer = await confirm_rolling_transfer(
            db,
            transfer_id=transfer_uuid,
            merchant_id=merchant.id,
            actor_id=user.id,
        )
    except (ValueError, RollingError) as exc:
        await db.rollback()
        return _redirect(str(exc), 'Пополнения')
    await db.commit()
    return _success_redirect('Пополнения')


@router.post('/cabinet/rolling/transfers/{transfer_id}/dispute')
async def cabinet_rolling_dispute_transfer(
    transfer_id: str,
    request: Request,
    reason: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    identity = await _merchant_for_rolling_action(request, db)
    if isinstance(identity, RedirectResponse):
        return identity
    user, merchant = identity
    try:
        transfer_uuid = uuid.UUID(transfer_id)
        transfer = await dispute_rolling_transfer(
            db,
            transfer_id=transfer_uuid,
            merchant_id=merchant.id,
            actor_id=user.id,
            reason=reason,
        )
    except (ValueError, RollingError) as exc:
        await db.rollback()
        return _redirect(str(exc), 'Пополнения')
    await db.commit()
    return _success_redirect('Пополнения')


@router.post('/cabinet/rolling/transfers/{transfer_id}/cancel')
async def cabinet_rolling_cancel_transfer(
    transfer_id: str,
    request: Request,
    reason: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    actor = await _require_superadmin_web(request, db)
    if isinstance(actor, RedirectResponse):
        return actor
    try:
        transfer_uuid = uuid.UUID(transfer_id)
        transfer = await cancel_rolling_transfer(
            db,
            transfer_id=transfer_uuid,
            actor_id=actor.id,
            reason=reason,
        )
    except (ValueError, RollingError) as exc:
        await db.rollback()
        return _redirect(str(exc), 'rolling')
    await db.commit()
    return _success_redirect('rolling')


@router.post('/cabinet/rolling/merchants/{merchant_id}/suspension')
async def cabinet_rolling_suspension(
    merchant_id: str,
    request: Request,
    suspended: bool = Form(False),
    reason: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    actor = await _require_superadmin_web(request, db)
    if isinstance(actor, RedirectResponse):
        return actor
    try:
        merchant_uuid = uuid.UUID(merchant_id)
        account = await set_rolling_suspension(
            db,
            merchant_id=merchant_uuid,
            suspended=suspended,
            actor_id=actor.id,
            reason=reason,
        )
    except (ValueError, RollingError) as exc:
        await db.rollback()
        return _redirect(str(exc), 'rolling')
    await audit(
        db,
        'rolling_account_suspended' if suspended else 'rolling_account_resumed',
        'merchant',
        actor.id,
        merchant_uuid,
        client_ip(request),
        {'status': account.status, 'reason': reason[:500]},
    )
    await db.commit()
    return _success_redirect('rolling')


def _teamlead_redirect(message: str) -> RedirectResponse:
    return RedirectResponse(
        cabinet_redirect_url('/cabinet', message=message, section='teamlead'),
        status_code=303,
    )


async def _require_teamlead_finance_web(
    request: Request,
    db: AsyncSession,
) -> User | RedirectResponse:
    actor = await get_current_web_user(request, db)
    if not actor:
        return RedirectResponse('/login', status_code=303)
    if actor.role != Role.teamlead.value:
        return _teamlead_redirect('Недостаточно прав')
    if staff_2fa_setup_required(actor):
        return _twofa_redirect()
    if not request.scope.get('session', {}).get('twofa_verified'):
        return _teamlead_redirect('Для финансового действия требуется подтверждённая 2FA-сессия')
    return actor


@router.post('/cabinet/teamlead/assignments')
async def teamlead_assignment_create(
    request: Request,
    teamlead_id: str = Form(...),
    trader_id: str = Form(...),
    commission_percent: str = Form(...),
    reason: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    actor = await _require_superadmin_web(request, db)
    if isinstance(actor, RedirectResponse):
        return actor
    try:
        assignment = await create_or_replace_assignment(
            db,
            teamlead_id=UUID(teamlead_id),
            trader_id=UUID(trader_id),
            commission_percent=Decimal(commission_percent.replace(',', '.')),
            actor_id=actor.id,
            reason=reason,
        )
        await audit(
            db,
            'teamlead_assignment_created',
            'teamlead_assignment',
            actor.id,
            assignment.id,
            client_ip(request),
            {
                'teamlead_id': str(assignment.teamlead_id),
                'trader_id': str(assignment.trader_id),
                'commission_percent': str(assignment.commission_percent),
                'effective_from': assignment.effective_from.isoformat(),
                'reason': reason[:500],
            },
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return _teamlead_redirect('teamlead_assignment_conflict')
    except (TeamLeadError, ValueError, InvalidOperation) as exc:
        await db.rollback()
        return _teamlead_redirect(str(exc))
    return _teamlead_redirect('Назначение TeamLead сохранено с историей')


@router.post('/cabinet/teamlead/merchant-assignments')
async def teamlead_merchant_assignment_create(
    request: Request,
    teamlead_id: str = Form(...),
    merchant_id: str = Form(...),
    commission_percent: str = Form(...),
    reason: str = Form(...),
    valid_from: str = Form(''),
    db: AsyncSession = Depends(get_db),
):
    actor = await _require_superadmin_web(request, db)
    if isinstance(actor, RedirectResponse):
        return actor
    try:
        assignment = await create_or_replace_merchant_assignment(
            db,
            teamlead_id=UUID(teamlead_id),
            merchant_id=UUID(merchant_id),
            commission_percent=Decimal(commission_percent.replace(',', '.')),
            actor_id=actor.id,
            reason=reason,
            valid_from=_fee_datetime(valid_from),
        )
        await audit(
            db,
            'teamlead_merchant_assignment_created',
            'teamlead_merchant_assignment',
            actor.id,
            assignment.id,
            client_ip(request),
            {
                'teamlead_id': str(assignment.teamlead_id),
                'merchant_id': str(assignment.merchant_id),
                'commission_percent': str(assignment.commission_percent),
                'valid_from': assignment.valid_from.isoformat(),
                'reason': reason[:500],
            },
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return _teamlead_redirect('teamlead_merchant_assignment_conflict')
    except (TeamLeadError, ValueError, InvalidOperation) as exc:
        await db.rollback()
        return _teamlead_redirect(str(exc))
    return _teamlead_redirect(
        'Назначение TeamLead мерчанту сохранено с историей'
    )


@router.post('/cabinet/teamlead/merchant-assignments/{merchant_id}/close')
async def teamlead_merchant_assignment_close(
    request: Request,
    merchant_id: str,
    reason: str = Form(...),
    valid_to: str = Form(''),
    db: AsyncSession = Depends(get_db),
):
    actor = await _require_superadmin_web(request, db)
    if isinstance(actor, RedirectResponse):
        return actor
    try:
        assignment = await close_merchant_assignment(
            db,
            merchant_id=UUID(merchant_id),
            actor_id=actor.id,
            reason=reason,
            valid_to=_fee_datetime(valid_to),
        )
        await audit(
            db,
            'teamlead_merchant_assignment_closed',
            'teamlead_merchant_assignment',
            actor.id,
            assignment.id,
            client_ip(request),
            {
                'teamlead_id': str(assignment.teamlead_id),
                'merchant_id': str(assignment.merchant_id),
                'valid_to': assignment.valid_to.isoformat(),
                'reason': reason[:500],
            },
        )
        await db.commit()
    except (TeamLeadError, ValueError) as exc:
        await db.rollback()
        return _teamlead_redirect(str(exc))
    return _teamlead_redirect('Назначение TeamLead мерчанту закрыто')


@router.post('/cabinet/teamlead/settlements/request')
async def teamlead_settlement_request(
    request: Request,
    requested_usdt: str = Form(...),
    wallet_address: str = Form(...),
    idempotency_key: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    actor = await _require_teamlead_finance_web(request, db)
    if isinstance(actor, RedirectResponse):
        return actor
    try:
        # No database row is locked while Rapira performs HTTP I/O.
        quote = await get_strict_rolling_ask_quote()
        settlement = await create_teamlead_settlement(
            db,
            teamlead_id=actor.id,
            requested_usdt=Decimal(requested_usdt.replace(',', '.')),
            wallet_address=wallet_address,
            idempotency_key=idempotency_key,
            quote=quote,
        )
        await audit(
            db,
            'teamlead_settlement_requested',
            'teamlead_settlement',
            actor.id,
            settlement.id,
            client_ip(request),
            {
                'requested_usdt': str(settlement.requested_usdt),
                'fee_usdt': str(settlement.fee_usdt),
                'total_debit_rub': str(settlement.total_debit_rub),
                'rate_symbol': settlement.rate_symbol,
                'rate_side': settlement.rate_side,
                'rate_source': settlement.rate_source,
                'provider_timestamp': (
                    settlement.provider_timestamp.isoformat()
                    if settlement.provider_timestamp
                    else None
                ),
                'fetched_at': settlement.fetched_at.isoformat(),
                'freshness_basis': settlement.freshness_basis,
            },
        )
        await db.commit()
    except RollingRateUnavailable:
        await db.rollback()
        return _teamlead_redirect('teamlead_rate_unavailable')
    except IntegrityError:
        await db.rollback()
        return _teamlead_redirect('teamlead_settlement_conflict')
    except (TeamLeadError, ValueError, InvalidOperation) as exc:
        await db.rollback()
        return _teamlead_redirect(str(exc))
    return _teamlead_redirect('Запрос TeamLead settle создан, средства заморожены')


@router.post('/cabinet/teamlead/settlements/{settlement_id}/reject')
async def teamlead_settlement_reject(
    request: Request,
    settlement_id: str,
    reason: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    actor = await _require_superadmin_web(request, db)
    if isinstance(actor, RedirectResponse):
        return actor
    try:
        settlement = await reject_teamlead_settlement(
            db,
            settlement_id=UUID(settlement_id),
            actor_id=actor.id,
            reason=reason,
        )
        await audit(
            db,
            'teamlead_settlement_rejected',
            'teamlead_settlement',
            actor.id,
            settlement.id,
            client_ip(request),
            {'reason': reason[:500]},
        )
        await db.commit()
    except (TeamLeadError, ValueError) as exc:
        await db.rollback()
        return _teamlead_redirect(str(exc))
    return _teamlead_redirect('TeamLead settle отклонён, frozen возвращён в available')


@router.post('/cabinet/teamlead/settlements/{settlement_id}/complete')
async def teamlead_settlement_complete(
    request: Request,
    settlement_id: str,
    tx_hash: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    actor = await _require_superadmin_web(request, db)
    if isinstance(actor, RedirectResponse):
        return actor
    try:
        settlement = await complete_teamlead_settlement(
            db,
            settlement_id=UUID(settlement_id),
            actor_id=actor.id,
            tx_hash=tx_hash,
        )
        await audit(
            db,
            'teamlead_settlement_completed',
            'teamlead_settlement',
            actor.id,
            settlement.id,
            client_ip(request),
            {'tx_hash': settlement.tx_hash},
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return _teamlead_redirect('teamlead_settlement_tx_hash_duplicate')
    except (TeamLeadError, ValueError) as exc:
        await db.rollback()
        return _teamlead_redirect(str(exc))
    return _teamlead_redirect('TeamLead settle выполнен, cooldown 168 часов запущен')


@router.post('/cabinet/teamlead/accruals/{deposit_id}/reverse')
async def teamlead_accrual_reverse(
    request: Request,
    deposit_id: str,
    reason: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    actor = await _require_superadmin_web(request, db)
    if isinstance(actor, RedirectResponse):
        return actor
    try:
        accruals = await reverse_teamlead_accruals_for_deposit(
            db,
            deposit_id=UUID(deposit_id),
            actor_id=actor.id,
            reason=reason,
        )
        if not accruals:
            raise TeamLeadError('teamlead_accrual_not_found')
        await audit(
            db,
            'teamlead_accrual_reversed',
            'deposit',
            actor.id,
            UUID(deposit_id),
            client_ip(request),
            {
                'deposit_id': deposit_id,
                'reason': reason[:500],
                'sources_reversed': [
                    'merchant_referral'
                    if isinstance(row, TeamLeadMerchantAccrual)
                    else 'trader_referral'
                    for row in accruals
                ],
            },
        )
        await db.commit()
    except (TeamLeadError, ValueError) as exc:
        await db.rollback()
        return _teamlead_redirect(str(exc))
    return _teamlead_redirect('TeamLead accrual компенсирован')


@router.post('/cabinet/teamlead/{teamlead_id}/adjust')
async def teamlead_balance_adjust(
    request: Request,
    teamlead_id: str,
    adjustment_type: str = Form(...),
    amount_rub: str = Form(...),
    reason: str = Form(...),
    idempotency_key: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    actor = await _require_superadmin_web(request, db)
    if isinstance(actor, RedirectResponse):
        return actor
    try:
        entry = await adjust_teamlead_balance(
            db,
            teamlead_id=UUID(teamlead_id),
            actor_id=actor.id,
            adjustment_type=adjustment_type,
            amount_rub=Decimal(amount_rub.replace(',', '.')),
            idempotency_key=idempotency_key,
            reason=reason,
        )
        await audit(
            db,
            'teamlead_balance_adjusted',
            'teamlead_ledger_entry',
            actor.id,
            entry.id,
            client_ip(request),
            {
                'teamlead_id': teamlead_id,
                'adjustment_type': adjustment_type,
                'amount_rub': str(entry.amount_rub),
                'reason': reason[:500],
            },
        )
        await db.commit()
    except (TeamLeadError, ValueError, InvalidOperation) as exc:
        await db.rollback()
        return _teamlead_redirect(str(exc))
    return _teamlead_redirect('Финансовая корректировка TeamLead записана в immutable ledger')


@router.get('/cabinet', response_class=HTMLResponse)
async def cabinet(request: Request, db: AsyncSession = Depends(get_db)):
    from app.presentation.tradespace.routes import tradespace_cabinet
    return await tradespace_cabinet(request, db)


@router.get('/cabinet/{role}', response_class=HTMLResponse)
async def cabinet_by_role(request: Request, role: str, db: AsyncSession = Depends(get_db)):
    if await _session_identity_changed(request):
        return _session_changed_page(request)
    user = await get_current_web_user(request, db)
    if not user:
        return RedirectResponse('/login', status_code=303)
    if user.role != role and user.role != Role.superadmin.value:
        return _preview_forbidden_page(request, user, role)
    trader_subject = None
    if role in TRADER_ROLES:
        trader_subject = await _trusted_trader_preview_subject(
            request,
            user,
            db,
            requested_role=role,
            accept_entry_token=True,
        )
        if not trader_subject:
            return _preview_forbidden_page(request, user, role)
    elif role != user.role:
        _clear_preview_context(request)
    from app.presentation.tradespace.routes import tradespace_cabinet, trader_preview
    if trader_subject and user.role == Role.superadmin.value:
        return await trader_preview(request, user, trader_subject, db)
    # Old role URLs are entry aliases only. They cannot render the former UI.
    return await tradespace_cabinet(request, db)


def _fee_datetime(value: str | None, *, default_now: bool = False) -> datetime | None:
    raw = (value or '').strip()
    if not raw:
        return datetime.now(timezone.utc) if default_now else None
    parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


FEE_ENTITY_ROUTE_SEGMENTS = {
    'merchant': 'merchants',
    'trader': 'traders',
    'aggregator': 'aggregators',
}


async def _fee_admin_user(request: Request, db: AsyncSession) -> User | None:
    user = await get_current_web_user(request, db)
    if user and user.role not in {Role.superadmin.value, Role.admin.value}:
        raise HTTPException(403, 'not enough permissions')
    return user


async def _load_active_fee_entity(db: AsyncSession, entity_type: str, entity_id: UUID):
    if entity_type == 'merchant':
        query = select(Merchant).join(User, User.id == Merchant.owner_id).where(
            Merchant.id == entity_id,
            Merchant.is_archived.is_(False),
            User.role == Role.merchant.value,
            User.is_active.is_(True),
            User.is_locked.is_(False),
            User.is_archived.is_(False),
        )
    elif entity_type == 'trader':
        query = select(User).where(
            User.id == entity_id,
            User.role.in_([Role.trader.value, Role.operator.value]),
            User.is_active.is_(True),
            User.is_locked.is_(False),
            User.is_archived.is_(False),
        )
    elif entity_type == 'aggregator':
        query = select(AggregatorAccount).where(
            AggregatorAccount.id == entity_id,
            AggregatorAccount.status == 'active',
            AggregatorAccount.is_archived.is_(False),
        )
    else:
        raise HTTPException(404, 'unknown fee entity type')
    entity = (await db.execute(query)).scalar_one_or_none()
    if entity is None:
        raise HTTPException(404, 'active fee entity not found')
    return entity


def _fee_side_for_entity(entity_type: str) -> str:
    return 'merchant_fee' if entity_type == 'merchant' else 'executor_fee'


COMMISSION_ENTITY_SECTIONS = {
    'merchant': 'Мерчанты',
    'trader': 'Трейдеры',
    'aggregator': 'Aggregators',
}


def _commission_tier_redirect(entity_type: str, entity_id: UUID, message: str) -> RedirectResponse:
    section = COMMISSION_ENTITY_SECTIONS[entity_type]
    selected = f'{entity_type}:{entity_id}'
    return RedirectResponse(
        cabinet_redirect_url(
            '/cabinet',
            message=message,
            section=section,
            extra={'commission_entity': selected},
        ),
        status_code=303,
    )


async def _cabinet_save_commission_tiers(
    request: Request,
    db: AsyncSession,
    *,
    entity_type: str,
    entity_id: UUID,
):
    user = await _fee_admin_user(request, db)
    if not user:
        return RedirectResponse('/login', status_code=303)
    await _load_active_fee_entity(db, entity_type, entity_id)
    form = await request.form()
    payment_method = str(form.get('payment_method') or 'sbp')
    raw_rates = {
        tier['field']: str(form.get(tier['field']) or '')
        for tier in COMMISSION_TIER_RANGES
    }
    changed_at = datetime.now(timezone.utc)
    try:
        old_values, rules = await replace_commission_tiers(
            db,
            actor_id=user.id,
            entity_type=entity_type,
            entity_id=entity_id,
            payment_method=payment_method,
            raw_rates=raw_rates,
            effective_at=changed_at,
        )
    except (FeeConfigurationError, ValueError) as exc:
        await db.rollback()
        return _commission_tier_redirect(
            entity_type,
            entity_id,
            f'Комиссии не сохранены: {exc}',
        )
    new_values = [
        {
            'rule_id': str(rule.id),
            'min_amount': str(rule.min_amount),
            'max_amount': str(rule.max_amount) if rule.max_amount is not None else None,
            'rate_percent': str(rule.rate_percent),
            'version': int(rule.version or 1),
        }
        for rule in rules
    ]
    await audit(
        db,
        'commission_tiers_saved',
        entity_type,
        user.id,
        entity_id,
        client_ip(request),
        {
            'entity_type': entity_type,
            'entity_id': str(entity_id),
            'fee_side': _fee_side_for_entity(entity_type),
            'payment_method': payment_method,
            'currency': 'RUB',
            'old': old_values,
            'new': new_values,
            'actor_id': str(user.id),
            'changed_at': changed_at.isoformat(),
        },
    )
    await db.commit()
    return _commission_tier_redirect(entity_type, entity_id, 'Комиссии сохранены')


def _fee_rule_form_values(form, *, entity_type: str, entity_id: UUID) -> dict:
    return {
        'entity_type': entity_type,
        'entity_id': entity_id,
        'fee_side': _fee_side_for_entity(entity_type),
        'payment_method': str(form.get('payment_method') or ''),
        'currency': str(form.get('currency') or 'RUB'),
        'min_amount': str(form.get('min_amount') or '0'),
        'max_amount': str(form.get('max_amount') or '') or None,
        'rate_percent': str(form.get('rate_percent') or ''),
        'effective_from': _fee_datetime(str(form.get('effective_from') or ''), default_now=True),
        'effective_to': _fee_datetime(str(form.get('effective_to') or '')),
    }


def _fee_entity_redirect(entity_type: str, entity_id: UUID, message: str) -> RedirectResponse:
    selected = f'{entity_type}:{entity_id}'
    return RedirectResponse(
        cabinet_redirect_url(
            '/cabinet',
            message=message,
            extra={'tariff_entity': selected},
            fragment='fee-tariffs',
        ),
        status_code=303,
    )


async def _cabinet_create_entity_fee_rule(
    request: Request,
    db: AsyncSession,
    *,
    entity_type: str,
    entity_id: UUID,
):
    user = await _fee_admin_user(request, db)
    if not user:
        return RedirectResponse('/login', status_code=303)
    await _load_active_fee_entity(db, entity_type, entity_id)
    form = await request.form()
    try:
        rule = await create_tier_fee_rule(
            db,
            actor_id=user.id,
            **_fee_rule_form_values(form, entity_type=entity_type, entity_id=entity_id),
        )
    except (FeeConfigurationError, ValueError) as exc:
        await db.rollback()
        return _fee_entity_redirect(entity_type, entity_id, f'Тариф не сохранён: {exc}')
    await audit(
        db, 'fee_rule_created', 'fee_rule', user.id, rule.id, client_ip(request),
        {
            'entity_type': rule.entity_type,
            'entity_id': str(rule.entity_id),
            'fee_side': rule.fee_side,
            'payment_method': rule.payment_method,
            'currency': rule.currency,
            'min_amount': str(rule.min_amount),
            'max_amount': str(rule.max_amount) if rule.max_amount is not None else None,
            'rate_percent': str(rule.rate_percent),
            'version': rule.version,
        },
    )
    await db.commit()
    return _fee_entity_redirect(entity_type, entity_id, 'Индивидуальный тариф создан')


async def _cabinet_replace_entity_fee_rule(
    request: Request,
    db: AsyncSession,
    *,
    entity_type: str,
    entity_id: UUID,
    rule_id: UUID,
):
    user = await _fee_admin_user(request, db)
    if not user:
        return RedirectResponse('/login', status_code=303)
    await _load_active_fee_entity(db, entity_type, entity_id)
    current = (await db.execute(
        select(FeeRule).where(
            FeeRule.id == rule_id,
            FeeRule.entity_type == entity_type,
            FeeRule.entity_id == entity_id,
            FeeRule.fee_side == _fee_side_for_entity(entity_type),
        ).with_for_update()
    )).scalar_one_or_none()
    if current is None:
        raise HTTPException(404, 'fee rule not found for this entity')
    if not current.is_active:
        raise HTTPException(409, 'only an active fee rule can be versioned')
    old_values = {
        'rule_id': str(current.id),
        'rate_percent': str(current.rate_percent),
        'min_amount': str(current.min_amount),
        'max_amount': str(current.max_amount) if current.max_amount is not None else None,
        'effective_from': current.effective_from.isoformat(),
        'effective_to': current.effective_to.isoformat() if current.effective_to else None,
        'version': current.version,
    }
    form = await request.form()
    try:
        replacement = await replace_tier_fee_rule(
            db,
            current,
            actor_id=user.id,
            **_fee_rule_form_values(form, entity_type=entity_type, entity_id=entity_id),
        )
    except (FeeConfigurationError, ValueError) as exc:
        await db.rollback()
        return _fee_entity_redirect(entity_type, entity_id, f'Новая версия не сохранена: {exc}')
    await audit(
        db, 'fee_rule_version_created', 'fee_rule', user.id, replacement.id, client_ip(request),
        {
            'entity_type': entity_type,
            'entity_id': str(entity_id),
            'old': old_values,
            'new': {
                'rule_id': str(replacement.id),
                'rate_percent': str(replacement.rate_percent),
                'min_amount': str(replacement.min_amount),
                'max_amount': str(replacement.max_amount) if replacement.max_amount is not None else None,
                'effective_from': replacement.effective_from.isoformat(),
                'effective_to': replacement.effective_to.isoformat() if replacement.effective_to else None,
                'version': replacement.version,
            },
        },
    )
    await db.commit()
    return _fee_entity_redirect(entity_type, entity_id, 'Создана новая версия тарифа')


async def _cabinet_deactivate_entity_fee_rule(
    request: Request,
    db: AsyncSession,
    *,
    entity_type: str,
    entity_id: UUID,
    rule_id: UUID,
):
    user = await _fee_admin_user(request, db)
    if not user:
        return RedirectResponse('/login', status_code=303)
    rule = (await db.execute(
        select(FeeRule).where(
            FeeRule.id == rule_id,
            FeeRule.entity_type == entity_type,
            FeeRule.entity_id == entity_id,
            FeeRule.fee_side == _fee_side_for_entity(entity_type),
        ).with_for_update()
    )).scalar_one_or_none()
    if rule is None:
        raise HTTPException(404, 'fee rule not found for this entity')
    if rule.is_active:
        rule.is_active = False
        rule.effective_to = min(rule.effective_to, datetime.now(timezone.utc)) if rule.effective_to else datetime.now(timezone.utc)
        rule.updated_by = user.id
        await audit(
            db, 'fee_rule_deactivated', 'fee_rule', user.id, rule.id, client_ip(request),
            {'entity_type': entity_type, 'entity_id': str(entity_id), 'version': rule.version},
        )
        await db.commit()
    return _fee_entity_redirect(entity_type, entity_id, 'Тариф деактивирован')


@router.post('/cabinet/merchants/{entity_id}/commission-tiers')
async def cabinet_save_merchant_commission_tiers(
    entity_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    return await _cabinet_save_commission_tiers(
        request, db, entity_type='merchant', entity_id=entity_id,
    )


@router.post('/cabinet/traders/{entity_id}/commission-tiers')
async def cabinet_save_trader_commission_tiers(
    entity_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    return await _cabinet_save_commission_tiers(
        request, db, entity_type='trader', entity_id=entity_id,
    )


@router.post('/cabinet/aggregators/{entity_id}/commission-tiers')
async def cabinet_save_aggregator_commission_tiers(
    entity_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    return await _cabinet_save_commission_tiers(
        request, db, entity_type='aggregator', entity_id=entity_id,
    )


@router.post('/cabinet/merchants/{entity_id}/tariffs')
async def cabinet_create_merchant_tariff(entity_id: UUID, request: Request, db: AsyncSession = Depends(get_db)):
    await _fee_admin_user(request, db)
    raise HTTPException(410, 'legacy tariff mutation is disabled; use the entity commission-tier card')


@router.post('/cabinet/traders/{entity_id}/tariffs')
async def cabinet_create_trader_tariff(entity_id: UUID, request: Request, db: AsyncSession = Depends(get_db)):
    await _fee_admin_user(request, db)
    raise HTTPException(410, 'legacy tariff mutation is disabled; use the entity commission-tier card')


@router.post('/cabinet/aggregators/{entity_id}/tariffs')
async def cabinet_create_aggregator_tariff(entity_id: UUID, request: Request, db: AsyncSession = Depends(get_db)):
    await _fee_admin_user(request, db)
    raise HTTPException(410, 'legacy tariff mutation is disabled; use the entity commission-tier card')


@router.post('/cabinet/merchants/{entity_id}/tariffs/{rule_id}/replace')
async def cabinet_replace_merchant_tariff(entity_id: UUID, rule_id: UUID, request: Request, db: AsyncSession = Depends(get_db)):
    await _fee_admin_user(request, db)
    raise HTTPException(410, 'legacy tariff mutation is disabled; use the entity commission-tier card')


@router.post('/cabinet/traders/{entity_id}/tariffs/{rule_id}/replace')
async def cabinet_replace_trader_tariff(entity_id: UUID, rule_id: UUID, request: Request, db: AsyncSession = Depends(get_db)):
    await _fee_admin_user(request, db)
    raise HTTPException(410, 'legacy tariff mutation is disabled; use the entity commission-tier card')


@router.post('/cabinet/aggregators/{entity_id}/tariffs/{rule_id}/replace')
async def cabinet_replace_aggregator_tariff(entity_id: UUID, rule_id: UUID, request: Request, db: AsyncSession = Depends(get_db)):
    await _fee_admin_user(request, db)
    raise HTTPException(410, 'legacy tariff mutation is disabled; use the entity commission-tier card')


@router.post('/cabinet/merchants/{entity_id}/tariffs/{rule_id}/deactivate')
async def cabinet_deactivate_merchant_tariff(entity_id: UUID, rule_id: UUID, request: Request, db: AsyncSession = Depends(get_db)):
    await _fee_admin_user(request, db)
    raise HTTPException(410, 'legacy tariff mutation is disabled; use the entity commission-tier card')


@router.post('/cabinet/traders/{entity_id}/tariffs/{rule_id}/deactivate')
async def cabinet_deactivate_trader_tariff(entity_id: UUID, rule_id: UUID, request: Request, db: AsyncSession = Depends(get_db)):
    await _fee_admin_user(request, db)
    raise HTTPException(410, 'legacy tariff mutation is disabled; use the entity commission-tier card')


@router.post('/cabinet/aggregators/{entity_id}/tariffs/{rule_id}/deactivate')
async def cabinet_deactivate_aggregator_tariff(entity_id: UUID, rule_id: UUID, request: Request, db: AsyncSession = Depends(get_db)):
    await _fee_admin_user(request, db)
    raise HTTPException(410, 'legacy tariff mutation is disabled; use the entity commission-tier card')


@router.post('/cabinet/fee-rules/create')
async def cabinet_create_fee_rule(request: Request, db: AsyncSession = Depends(get_db)):
    user = await _fee_admin_user(request, db)
    if not user:
        return RedirectResponse('/login', status_code=303)
    raise HTTPException(410, 'generic fee-rule creation is disabled; use an entity-scoped tariff route')


@router.post('/cabinet/fee-rules/{rule_id}/deactivate')
async def cabinet_deactivate_fee_rule(rule_id: UUID, request: Request, db: AsyncSession = Depends(get_db)):
    user = await _fee_admin_user(request, db)
    if not user:
        return RedirectResponse('/login', status_code=303)
    raise HTTPException(410, 'unscoped fee-rule deactivation is disabled; use an entity-scoped tariff route')


def _trader_requisite_search_text(req: Requisite) -> str:
    return ' '.join([
        display_requisite_value(req),
        req.bank_code or '',
        req.bank_name or '',
        req.operator_code or '',
        req.full_name or '',
        req.owner_name or '',
    ]).lower()


async def _load_trader_deposit_view(
    db: AsyncSession,
    *,
    subject_trader_id: UUID,
    filters: dict[str, str],
    page_size: int = 500,
) -> dict[str, object]:
    """Load one Trader subject's SQL-scoped deposit view before the page limit."""
    requisites = (await db.execute(
        select(Requisite)
        .where(
            Requisite.trader_id == subject_trader_id,
            Requisite.is_archived.is_(False),
        )
        .order_by(Requisite.created_at.desc())
    )).scalars().all()
    requisite_ids = [row.id for row in requisites]
    query_text = filters.get('query', '').lower()
    requisite_text = filters.get('requisite', '').lower()
    matching_requisite_ids = [
        req.id for req in requisites
        if query_text and query_text in _trader_requisite_search_text(req)
    ]
    requisite_filter_ids = [
        req.id for req in requisites
        if requisite_text and requisite_text in _trader_requisite_search_text(req)
    ]
    bank_requisite_ids = [
        req.id for req in requisites
        if filters.get('bank_code') and req.bank_code == filters['bank_code']
    ]
    statement = apply_trader_deposit_filters(
        trader_deposit_scope(requisite_ids),
        filters,
        matching_requisite_ids=matching_requisite_ids,
        requisite_filter_ids=requisite_filter_ids,
        bank_requisite_ids=bank_requisite_ids,
    ).limit(max(1, min(int(page_size), 500)))
    deposits = (await db.execute(statement)).scalars().all() if requisite_ids else []
    aggregates = await trader_deposit_aggregates(db, requisite_ids=requisite_ids)
    return {
        'requisites': requisites,
        'deposits': deposits,
        'aggregates': aggregates,
    }


def _can_render_trader_deposit_process(
    viewer: User,
    subject: User,
    deposit: Deposit,
    requisite: Requisite | None,
) -> bool:
    if deposit.status not in DEPOSIT_ACTIVE_STATUSES:
        return False
    if not requisite or str(requisite.trader_id) != str(subject.id):
        return False
    if viewer.role in {Role.superadmin.value, Role.admin.value}:
        return True
    return viewer.role in TRADER_ROLES and str(viewer.id) == str(subject.id)


def _trader_deposit_details(
    viewer: User,
    subject: User,
    deposits: list[Deposit],
    requisites: list[Requisite],
) -> dict[str, dict[str, object]]:
    req_by_id = {row.id: row for row in requisites}
    now = datetime.now(timezone.utc)
    details = {}
    for deposit in deposits:
        req = req_by_id.get(deposit.requisites_id)
        deadline = _deposit_deadline(deposit)
        details[str(deposit.id)] = {
            'requisite': display_requisite_value(req) if req and _can_view_requisite_value(viewer, req) else '',
            'bank': requisite_provider_display(req.method, req.bank_code, req.operator_code, req.bank_name) if req else '',
            'provider_label': requisite_provider_label(req.method) if req else 'Банк',
            'provider_name': requisite_provider_display(req.method, req.bank_code, req.operator_code, req.bank_name) if req else '',
            'receiver_name': (req.full_name or req.owner_name) if req else '',
            'deadline_iso': deadline.isoformat(),
            'remaining_seconds': max(0, int((deadline - now).total_seconds())),
            'is_active': deposit.status in DEPOSIT_ACTIVE_FOR_TRADER,
            'failure_reason': (deposit.metadata_json or {}).get('failure_reason', ''),
            'can_process': _can_render_trader_deposit_process(
                viewer,
                subject,
                deposit,
                req,
            ),
        }
    return details


async def _deposit_partial_context(request: Request, user: User, db: AsyncSession) -> dict:
    role = user.role
    auth_realm = request_auth_realm(request) or realm_for_role(role)
    cabinet_base = realm_cabinet_path(auth_realm) if auth_realm else '/cabinet'
    merchant = None
    if role == Role.merchant.value:
        merchant = (await db.execute(
            select(Merchant).where(Merchant.owner_id == user.id, Merchant.is_archived.is_(False))
        )).scalar_one_or_none()
    trader_subject = await _trusted_trader_preview_subject(
        request,
        user,
        db,
        requested_role=Role.trader.value,
        accept_entry_token=False,
    )
    preview_requested = request.query_params.get('preview', '').strip() == 'trader'
    if preview_requested and not trader_subject:
        raise HTTPException(403, 'trusted Trader preview context required')
    if trader_subject:
        filters = normalized_deposit_filters(
            request.query_params,
            valid_bank_codes=(bank.code for bank in get_enabled_banks()),
        )
        view_data = await _load_trader_deposit_view(
            db,
            subject_trader_id=trader_subject.id,
            filters=filters,
        )
        requisites = view_data['requisites']
        deposits = view_data['deposits']
        return {
            'request': request,
            'role': user.role if user.role in TRADER_ROLES else Role.trader.value,
            'deposits': deposits,
            'deposit_details': _trader_deposit_details(
                user,
                trader_subject,
                deposits,
                requisites,
            ),
            'deposit_filters': filters,
            'cabinet_base': cabinet_base,
            **branding_context(),
        }
    staff_roles = {
        Role.superadmin.value, Role.admin.value, Role.support.value,
    }
    if role in staff_roles:
        deposits = (await db.execute(
            select(Deposit).order_by(Deposit.created_at.desc()).limit(500)
        )).scalars().all()
        requisites = []
    elif merchant:
        deposits = (await db.execute(
            select(Deposit).where(Deposit.merchant_id == merchant.id)
            .order_by(Deposit.created_at.desc()).limit(500)
        )).scalars().all()
        requisites = []
    else:
        deposits = []
        requisites = []

    req_by_id = {row.id: row for row in requisites}
    missing_ids = {
        row.requisites_id for row in deposits
        if row.requisites_id and row.requisites_id not in req_by_id
    }
    if missing_ids:
        rows = (await db.execute(select(Requisite).where(Requisite.id.in_(missing_ids)))).scalars().all()
        req_by_id.update({row.id: row for row in rows})
    if role not in {Role.operator.value, Role.trader.value}:
        filters = {
            'query': request.query_params.get('deposit_query', '').strip()
            or request.query_params.get('merchant_deal_id', '').strip(),
            'amount': request.query_params.get('deposit_amount', '').strip(),
            'requisite': request.query_params.get('deposit_requisite', '').strip(),
            'date': request.query_params.get('deposit_date', '').strip(),
        }
    merchant_trader_id = request.query_params.get('merchant_trader_id', '').strip()
    if merchant_trader_id and role == Role.merchant.value:
        deposits = [
            row for row in deposits
            if row.requisites_id and req_by_id.get(row.requisites_id)
            and str(req_by_id[row.requisites_id].trader_id) == merchant_trader_id
        ]
    if any(filters.values()) and role not in {Role.operator.value, Role.trader.value}:
        deposits = [
            row for row in deposits
            if deposit_matches_filters(row, req_by_id.get(row.requisites_id), filters)
        ]
    trader_ids = {req.trader_id for req in req_by_id.values() if req.trader_id}
    traders = (await db.execute(select(User).where(User.id.in_(trader_ids)))).scalars().all() if trader_ids else []
    trader_by_id = {row.id: row for row in traders}
    now = datetime.now(timezone.utc)
    details = {}
    for deposit in deposits:
        req = req_by_id.get(deposit.requisites_id)
        trader = trader_by_id.get(req.trader_id) if req else None
        deadline = _deposit_deadline(deposit)
        merchant_related = bool(merchant and deposit.merchant_id == merchant.id)
        details[str(deposit.id)] = {
            'requisite': display_requisite_value(req) if req and _can_view_requisite_value(
                user, req, merchant_related=merchant_related
            ) else '',
            'bank': requisite_provider_display(req.method, req.bank_code, req.operator_code, req.bank_name) if req else '',
            'provider_label': requisite_provider_label(req.method) if req else 'Банк',
            'provider_name': requisite_provider_display(req.method, req.bank_code, req.operator_code, req.bank_name) if req else '',
            'receiver_name': (req.full_name or req.owner_name) if req else '',
            'remaining_seconds': max(0, int((deadline - now).total_seconds())),
            'is_active': deposit.status in DEPOSIT_ACTIVE_FOR_TRADER,
            'trader_email': trader.email if trader else '',
            'trader_profit': str(deposit_trader_profit_amount(deposit, trader)),
            'trader_settlement': str(deposit_trader_settlement_amount(deposit, trader)),
            'failure_reason': (deposit.metadata_json or {}).get('failure_reason', ''),
        }
    return {
        'request': request, 'role': role, 'deposits': deposits,
        'deposit_details': details, 'deposit_filters': filters,
        'cabinet_base': cabinet_base,
        **branding_context(),
    }


@router.get('/cabinet/partials/deposits', response_class=HTMLResponse)
async def cabinet_deposit_rows(request: Request, db: AsyncSession = Depends(get_db)):
    if await _session_identity_changed(request):
        return JSONResponse(
            {'code': SESSION_IDENTITY_CHANGED_CODE, 'message': SESSION_IDENTITY_CHANGED_MESSAGE},
            status_code=409,
        )
    user = await get_current_web_user(request, db)
    if not user:
        return JSONResponse({'code': 'authentication_required'}, status_code=401)
    return templates.TemplateResponse(
        request=request,
        name='_deposit_rows.html',
        context=await _deposit_partial_context(request, user, db),
        headers={'Cache-Control': 'no-store'},
    )


async def _teamlead_own_cabinet(
    request: Request,
    user: User,
    db: AsyncSession,
) -> HTMLResponse:
    auth_realm = request_auth_realm(request) or realm_for_role(user.role)
    cabinet_base = realm_cabinet_path(auth_realm) if auth_realm else '/cabinet'
    balance = (await db.execute(
        select(TeamLeadBalance).where(TeamLeadBalance.teamlead_id == user.id)
    )).scalar_one_or_none()
    if balance is None:
        balance = TeamLeadBalance(
            teamlead_id=user.id,
            available_rub=Decimal('0.00'),
            frozen_rub=Decimal('0.00'),
            debt_rub=Decimal('0.00'),
            total_earned_rub=Decimal('0.00'),
            total_paid_rub=Decimal('0.00'),
        )
    assignments = (await db.execute(
        select(TeamLeadTraderAssignment)
        .where(
            TeamLeadTraderAssignment.teamlead_id == user.id,
            TeamLeadTraderAssignment.effective_to.is_(None),
        )
        .order_by(TeamLeadTraderAssignment.effective_from)
    )).scalars().all()
    merchant_assignments = (await db.execute(
        select(TeamLeadMerchantAssignment)
        .where(
            TeamLeadMerchantAssignment.teamlead_id == user.id,
            TeamLeadMerchantAssignment.valid_to.is_(None),
        )
        .order_by(TeamLeadMerchantAssignment.valid_from)
    )).scalars().all()
    accruals = (await db.execute(
        select(TeamLeadAccrual)
        .where(TeamLeadAccrual.teamlead_id == user.id)
        .order_by(TeamLeadAccrual.created_at.desc())
        .limit(500)
    )).scalars().all()
    merchant_accruals = (await db.execute(
        select(TeamLeadMerchantAccrual)
        .where(TeamLeadMerchantAccrual.teamlead_id == user.id)
        .order_by(TeamLeadMerchantAccrual.created_at.desc())
        .limit(500)
    )).scalars().all()
    trader_ids = {
        *(row.trader_id for row in assignments),
        *(row.trader_id for row in accruals),
    }
    traders = []
    if trader_ids:
        traders = (await db.execute(
            select(User.id, User.email, User.is_active, User.is_locked)
            .where(User.id.in_(trader_ids))
            .order_by(User.email)
        )).all()
    trader_labels = {row[0]: row[1] for row in traders}
    merchant_ids = {
        *(row.merchant_id for row in merchant_assignments),
        *(row.merchant_id for row in merchant_accruals),
    }
    merchant_labels = {}
    if merchant_ids:
        merchant_labels = dict((await db.execute(
            select(Merchant.id, Merchant.name).where(
                Merchant.id.in_(merchant_ids)
            )
        )).all())
    settlements = (await db.execute(
        select(TeamLeadSettlement)
        .where(TeamLeadSettlement.teamlead_id == user.id)
        .order_by(TeamLeadSettlement.requested_at.desc())
        .limit(200)
    )).scalars().all()
    ledger = (await db.execute(
        select(TeamLeadLedgerEntry)
        .where(TeamLeadLedgerEntry.teamlead_id == user.id)
        .order_by(TeamLeadLedgerEntry.created_at.desc())
        .limit(300)
    )).scalars().all()
    stats_by_trader: dict[str, dict] = {}
    for accrual in accruals:
        item = stats_by_trader.setdefault(
            str(accrual.trader_id),
            {
                'gross_rub': Decimal('0.00'),
                'accrual_rub': Decimal('0.00'),
                'operations': 0,
            },
        )
        item['gross_rub'] += Decimal(accrual.gross_rub)
        if accrual.status == 'credited':
            item['accrual_rub'] += Decimal(accrual.accrual_rub)
        item['operations'] += 1
    trader_rows = [
        {
            'id': str(assignment.trader_id),
            'email': trader_labels.get(assignment.trader_id, 'Archived trader'),
            'commission_percent': assignment.commission_percent,
            **stats_by_trader.get(
                str(assignment.trader_id),
                {
                    'gross_rub': Decimal('0.00'),
                    'accrual_rub': Decimal('0.00'),
                    'operations': 0,
                },
            ),
        }
        for assignment in assignments
    ]
    stats_by_merchant: dict[str, dict] = {}
    for accrual in merchant_accruals:
        item = stats_by_merchant.setdefault(
            str(accrual.merchant_id),
            {
                'gross_rub': Decimal('0.00'),
                'accrual_rub': Decimal('0.00'),
                'operations': 0,
            },
        )
        item['gross_rub'] += Decimal(accrual.gross_rub)
        if accrual.status == 'credited':
            item['accrual_rub'] += Decimal(accrual.accrual_rub)
        item['operations'] += 1
    merchant_rows = [
        {
            'name': merchant_labels.get(
                assignment.merchant_id,
                'Архивный мерчант',
            ),
            'commission_percent': assignment.commission_percent,
            **stats_by_merchant.get(
                str(assignment.merchant_id),
                {
                    'gross_rub': Decimal('0.00'),
                    'accrual_rub': Decimal('0.00'),
                    'operations': 0,
                },
            ),
        }
        for assignment in merchant_assignments
    ]
    completed_times = [
        row.completed_at
        for row in settlements
        if row.status == 'completed' and row.completed_at is not None
    ]
    next_settlement_at = (
        max(completed_times) + TEAMLEAD_SETTLEMENT_COOLDOWN
        if completed_times
        else None
    )
    pending_settlement = next(
        (row for row in settlements if row.status == 'pending'),
        None,
    )
    reconciliation = (
        await reconcile_teamlead_account(db, user.id)
        if getattr(balance, 'id', None)
        else {
            'reconciled': True,
            'checks': {},
            'totals': {},
            'balance_cache': {
                'available_rub': Decimal('0.00'),
                'frozen_rub': Decimal('0.00'),
                'debt_rub': Decimal('0.00'),
                'total_earned_rub': Decimal('0.00'),
                'total_paid_rub': Decimal('0.00'),
            },
        }
    )
    return templates.TemplateResponse(
        request=request,
        name='teamlead.html',
        context={
            'request': request,
            **branding_context(),
            'user': user,
            'role': Role.teamlead.value,
            'title': ROLE_TITLES[Role.teamlead.value],
            'description': ROLE_DESCRIPTIONS[Role.teamlead.value],
            'auth_realm': auth_realm,
            'cabinet_base': cabinet_base,
            'logout_path': f'/{auth_realm}/logout' if auth_realm else '/logout',
            'ui_message': resolve_ui_message(request.query_params),
            'balance': balance,
            'trader_rows': trader_rows,
            'merchant_rows': merchant_rows,
            'teamlead_trader_labels': trader_labels,
            'teamlead_merchant_labels': merchant_labels,
            'accruals': accruals,
            'merchant_accruals': merchant_accruals,
            'settlements': settlements,
            'ledger': ledger,
            'pending_settlement': pending_settlement,
            'next_settlement_at': next_settlement_at,
            'total_gross_rub': sum(
                (
                    Decimal(row.gross_rub)
                    for row in [*accruals, *merchant_accruals]
                ),
                Decimal('0.00'),
            ),
            'trader_gross_rub': sum(
                (Decimal(row.gross_rub) for row in accruals),
                Decimal('0.00'),
            ),
            'merchant_gross_rub': sum(
                (Decimal(row.gross_rub) for row in merchant_accruals),
                Decimal('0.00'),
            ),
            'trader_income_rub': sum(
                (
                    Decimal(row.accrual_rub)
                    for row in accruals
                    if row.status == 'credited'
                ),
                Decimal('0.00'),
            ),
            'merchant_income_rub': sum(
                (
                    Decimal(row.accrual_rub)
                    for row in merchant_accruals
                    if row.status == 'credited'
                ),
                Decimal('0.00'),
            ),
            'reconciliation': reconciliation,
            'settlement_idempotency_key': (
                f'teamlead-settlement:{user.id}:{secrets.token_urlsafe(18)}'
            ),
        },
        headers={'Cache-Control': 'no-store'},
    )


async def role_cabinet(
    request: Request,
    role: str,
    db: AsyncSession,
    *,
    trader_subject: User | None = None,
):
    user = await get_current_web_user(request, db)
    if role in TRADER_ROLES and trader_subject is None and user.role in TRADER_ROLES:
        trader_subject = user
    trader_preview_active = bool(
        trader_subject and user.role == Role.superadmin.value and role in TRADER_ROLES
    )
    if (
        role == Role.teamlead.value
        and user.role == Role.teamlead.value
        and not staff_2fa_setup_required(user)
    ):
        return await _teamlead_own_cabinet(request, user, db)
    auth_realm = request_auth_realm(request) or realm_for_role(user.role)
    cabinet_base = realm_cabinet_path(auth_realm) if auth_realm else '/cabinet'
    sections = [section for section in ROLE_SECTIONS.get(role, []) if section not in HIDDEN_CABINET_SECTIONS]
    ui_message = resolve_ui_message(request.query_params)
    if staff_2fa_setup_required(user):
        role = user.role
        sections = ['Безопасность']
        ui_message = ui_message or resolve_ui_message({'ui_error': 'twofa_required'})
    elif not trader_preview_active:
        expired_events = await approve_expired_appeals(db)
        if expired_events:
            await db.commit()
            for expired_event in expired_events:
                enqueue_webhook_delivery(expired_event.id)

    merchant = None
    merchant_api_keys = []
    merchant_api_base_url = str(request.base_url).rstrip('/') + '/api/v1/merchant'
    merchant_local_docker_api_base_url = 'http://host.docker.internal:8000/api/v1/merchant'
    balance = None
    settlement_quote = {}
    merchant_settlement_idempotency_key = ''
    merchant_settlements = []
    webhook_events = []
    attempts_by_event_id = {}
    webhook_event_views = []
    aggregator_accounts = []
    aggregator_payments = []
    aggregator_callback_logs = []
    aggregator_name_by_id = {}
    aggregator_secret_flash = await _consume_aggregator_secret_flash(request, user)
    antiscam = {}
    valid_risk_states = {member.value for member in TrafficStatus}
    valid_risk_methods = {member.value for member in PaymentMethod}
    valid_risk_bank_codes = {bank.code for bank in get_enabled_banks()}
    risk_filters = {
        'query': request.query_params.get('risk_query', '').strip(),
        'trader_id': request.query_params.get('risk_trader_id', '').strip(),
        'bank_code': request.query_params.get('risk_bank_code', '').strip(),
        'payment_method': request.query_params.get('risk_payment_method', '').strip(),
        'state': request.query_params.get('risk_state', '').strip(),
    }
    if risk_filters['bank_code'] not in valid_risk_bank_codes:
        risk_filters['bank_code'] = ''
    if risk_filters['payment_method'] not in valid_risk_methods:
        risk_filters['payment_method'] = ''
    if risk_filters['state'] not in valid_risk_states:
        risk_filters['state'] = ''
    settlement_merchant_names = {}
    merchant_filter_trader_id = request.query_params.get('merchant_trader_id', '').strip()
    merchant_deal_id = request.query_params.get('merchant_deal_id', '').strip()
    if role in {Role.operator.value, Role.trader.value}:
        deposit_filters = normalized_deposit_filters(
            request.query_params,
            valid_bank_codes=(bank.code for bank in get_enabled_banks()),
        )
    else:
        deposit_filters = {
            'view': '',
            'query': (request.query_params.get('deposit_query', '').strip() or merchant_deal_id),
            'amount': request.query_params.get('deposit_amount', '').strip(),
            'requisite': request.query_params.get('deposit_requisite', '').strip(),
            'date': request.query_params.get('deposit_date', '').strip(),
            'bank_code': '',
            'payment_method': '',
            'status': '',
        }
    deposit_aggregates = {}
    appeal_filters = {
        'query': request.query_params.get('appeal_query', '').strip(),
        'amount': request.query_params.get('appeal_amount', '').strip(),
        'requisite': request.query_params.get('appeal_requisite', '').strip(),
        'date': request.query_params.get('appeal_date', '').strip(),
    }
    merchant_traders = []
    merchant_all_deposits = []
    merchant_stats = {}
    merchant_trader_rows = []
    fee_rules = []
    fee_entity_options = []
    fee_rule_entity_labels = {}
    fee_rule_actor_labels = {}
    selected_fee_entity = None
    selected_fee_rules = []
    commission_tiers_by_entity = {}
    income_merchant_options = []
    income_trader_options = []
    income_aggregator_options = []
    income_dashboard = {}
    merchant_rolling_overview = {
        'has_confirmed_rolling': False,
        'status': 'absent',
        'principal': Decimal('0.000000'),
        'recovered': Decimal('0.000000'),
        'outstanding': Decimal('0.000000'),
        'pending_transfers': 0,
        'disputed_transfers': 0,
        'pending_exposure': Decimal('0.000000'),
    }
    merchant_rolling_allocations = []
    merchant_rolling_ledger = []
    merchant_rolling_transfers = []
    merchant_rolling_consumptions = []
    rolling_admin_rows = []
    merchant_finance_rows = {}
    teamlead_admin_rows = []
    merchant_teamlead_assignments = {}
    teamlead_user_labels = {}
    platform_wallet = None
    platform_wallet_history = []
    platform_wallet_actor_labels = {}
    platform_wallet_qr_url = ''
    ai_office_config = ai_config_view(None)
    if user.role in {
        Role.superadmin.value,
        Role.admin.value,
        Role.operator.value,
        Role.trader.value,
    }:
        platform_wallet = await get_active_platform_wallet(db)
        if platform_wallet:
            platform_wallet_qr_url = (
                f'{cabinet_base}/platform-wallet/qr'
                f'?v={platform_wallet.version}'
            )
        if user.role in {Role.superadmin.value, Role.admin.value}:
            platform_wallet_history = await list_platform_wallet_history(db)
            actor_ids = {
                actor_id
                for wallet in platform_wallet_history
                for actor_id in (wallet.created_by, wallet.deactivated_by)
                if actor_id is not None
            }
            if actor_ids:
                actor_rows = (
                    await db.execute(
                        select(User.id, User.email).where(
                            User.id.in_(actor_ids)
                        )
                    )
                ).all()
                platform_wallet_actor_labels = {
                    actor_id: email
                    for actor_id, email in actor_rows
                }
    if user.role == Role.superadmin.value:
        ai_office_config = ai_config_view(
            await get_ai_integration_config(db)
        )
    if user.role in {Role.superadmin.value, Role.admin.value}:
        fee_rules = (await db.execute(
            select(FeeRule).order_by(FeeRule.is_active.desc(), FeeRule.created_at.desc()).limit(500)
        )).scalars().all()
        active_commission_rules = (await db.execute(
            select(FeeRule).where(
                FeeRule.is_active.is_(True),
                FeeRule.currency == 'RUB',
                FeeRule.payment_method.in_(COMMISSION_TIER_METHODS),
            ).order_by(FeeRule.version.desc(), FeeRule.created_at.desc())
        )).scalars().all()
        commission_tiers_by_entity = build_commission_tier_context(active_commission_rules)
        income_date_from_raw = request.query_params.get('income_date_from', '').strip()
        income_date_to_raw = request.query_params.get('income_date_to', '').strip()
        income_merchant_raw = request.query_params.get('income_merchant_id', '').strip()
        income_trader_raw = request.query_params.get('income_trader_id', '').strip()
        income_aggregator_raw = request.query_params.get('income_aggregator_id', '').strip()
        income_amount_min_raw = request.query_params.get('income_amount_min', '').strip()
        income_amount_max_raw = request.query_params.get('income_amount_max', '').strip()
        income_page_raw = request.query_params.get('income_page', '1').strip()
        income_page = int(income_page_raw) if income_page_raw.isdigit() else 1
        try:
            income_date_from = _fee_datetime(income_date_from_raw)
            income_date_to = _fee_datetime(income_date_to_raw)
            if income_date_to is not None and len(income_date_to_raw) == 10:
                income_date_to += timedelta(days=1)
            income_merchant_id = UUID(income_merchant_raw) if income_merchant_raw else None
            income_trader_id = UUID(income_trader_raw) if income_trader_raw else None
            income_aggregator_id = UUID(income_aggregator_raw) if income_aggregator_raw else None
            income_amount_min = Decimal(income_amount_min_raw) if income_amount_min_raw else None
            income_amount_max = Decimal(income_amount_max_raw) if income_amount_max_raw else None
        except (ValueError, ArithmeticError):
            income_date_from = None
            income_date_to = None
            income_merchant_id = None
            income_trader_id = None
            income_aggregator_id = None
            income_amount_min = None
            income_amount_max = None
        income_dashboard = await platform_income_dashboard(
            db,
            date_from=income_date_from,
            date_to=income_date_to,
            merchant_id=income_merchant_id,
            trader_id=income_trader_id,
            aggregator_id=income_aggregator_id,
            payment_method=request.query_params.get('income_payment_method', '').strip() or None,
            currency=request.query_params.get('income_currency', '').strip() or None,
            executor_type=request.query_params.get('income_executor_type', '').strip() or None,
            operation_status=request.query_params.get('income_operation_status', '').strip() or None,
            settlement_status=request.query_params.get('income_settlement_status', '').strip() or None,
            amount_min=income_amount_min,
            amount_max=income_amount_max,
            margin=request.query_params.get('income_margin', '').strip() or None,
            page=max(1, income_page),
            page_size=50,
        )
        income_dashboard['date_from_value'] = income_date_from_raw
        income_dashboard['date_to_value'] = income_date_to_raw
    if role == Role.merchant.value:
        merchant = (await db.execute(select(Merchant).where(Merchant.owner_id == user.id))).scalar_one_or_none()
        if merchant:
            balance = (await db.execute(
                select(Balance).where(
                    Balance.merchant_id == merchant.id,
                    Balance.currency == 'RUB',
                )
            )).scalar_one_or_none()
            settlement_quote = await merchant_settlement_quote(
                db,
                merchant.id,
                allow_unavailable=True,
            )
            merchant_settlement_idempotency_key = (
                f'merchant-settlement:{merchant.id}:{uuid.uuid4().hex}'
            )
            merchant_api_keys = (await db.execute(select(ApiKey).where(ApiKey.merchant_id == merchant.id).order_by(ApiKey.is_active.desc(), ApiKey.created_at.desc()).limit(20))).scalars().all()
            merchant_rolling_overview = await rolling_overview(db, merchant.id)
            merchant_rolling_allocations = (await db.execute(
                select(MerchantRollingAllocation)
                .where(MerchantRollingAllocation.merchant_id == merchant.id)
                .order_by(MerchantRollingAllocation.created_at.desc())
                .limit(200)
            )).scalars().all()
            merchant_rolling_ledger = (await db.execute(
                select(MerchantRollingLedgerEntry)
                .where(MerchantRollingLedgerEntry.merchant_id == merchant.id)
                .order_by(MerchantRollingLedgerEntry.created_at.desc())
                .limit(200)
            )).scalars().all()
            merchant_rolling_transfers = await list_rolling_transfers(
                db,
                merchant.id,
            )
            merchant_rolling_consumptions = (await db.execute(
                select(MerchantRollingTransferConsumption)
                .where(
                    MerchantRollingTransferConsumption.merchant_id
                    == merchant.id
                )
                .order_by(
                    MerchantRollingTransferConsumption.created_at.desc()
                )
                .limit(300)
            )).scalars().all()

    users = (await db.execute(select(User).where(User.is_archived.is_(False)).order_by(User.created_at.desc()).limit(300))).scalars().all() if user.role in [Role.superadmin.value, Role.admin.value] else []
    merchant_users = [u for u in users if u.role == Role.merchant.value]
    trader_users = (await db.execute(select(User).where(User.role.in_([Role.operator.value, Role.trader.value]), User.is_archived.is_(False)).order_by(User.created_at.desc()).limit(300))).scalars().all() if user.role in [Role.superadmin.value, Role.admin.value, Role.support.value] else []
    trader_preview_urls = {}
    if user.role == Role.superadmin.value:
        for preview_trader in trader_users:
            preview_token = issue_preview_context_token(
                request.session,
                preview_role=Role.trader.value,
                subject_id=str(preview_trader.id),
            )
            if preview_token:
                trader_preview_urls[str(preview_trader.id)] = (
                    f'{cabinet_base}/{Role.trader.value}'
                    f'?preview_context={quote_plus(preview_token)}'
                    '&section=deposits&view=active'
                )
    merchants = (await db.execute(select(Merchant).where(Merchant.is_archived.is_(False)).order_by(Merchant.created_at.desc()).limit(300))).scalars().all() if user.role in [Role.superadmin.value, Role.admin.value, Role.support.value] else ([merchant] if merchant else [])
    if user.role in {Role.superadmin.value, Role.admin.value}:
        teamlead_users = [row for row in users if row.role == Role.teamlead.value]
        teamlead_ids = {row.id for row in teamlead_users}
        teamlead_user_labels = {row.id: row.email for row in teamlead_users}
        assignment_rows = []
        merchant_assignment_rows = []
        if teamlead_ids:
            assignment_rows = (await db.execute(
                select(TeamLeadTraderAssignment)
                .where(TeamLeadTraderAssignment.teamlead_id.in_(teamlead_ids))
                .order_by(
                    TeamLeadTraderAssignment.effective_from.desc(),
                    TeamLeadTraderAssignment.id.desc(),
                )
            )).scalars().all()
        merchant_ids_for_history = {row.id for row in merchants}
        if merchant_ids_for_history:
            merchant_assignment_rows = (await db.execute(
                select(TeamLeadMerchantAssignment)
                .where(
                    TeamLeadMerchantAssignment.merchant_id.in_(
                        merchant_ids_for_history
                    )
                )
                .order_by(
                    TeamLeadMerchantAssignment.valid_from.desc(),
                    TeamLeadMerchantAssignment.id.desc(),
                )
            )).scalars().all()
        historical_teamlead_ids = {
            row.teamlead_id for row in merchant_assignment_rows
        }
        missing_teamlead_ids = historical_teamlead_ids.difference(
            teamlead_user_labels
        )
        if missing_teamlead_ids:
            teamlead_user_labels.update(dict((await db.execute(
                select(User.id, User.email).where(
                    User.id.in_(missing_teamlead_ids)
                )
            )).all()))
        for assignment in merchant_assignment_rows:
            merchant_teamlead_assignments.setdefault(
                str(assignment.merchant_id),
                [],
            ).append(assignment)
        teamlead_merchant_ids = {
            row.merchant_id for row in merchant_assignment_rows
        }
        teamlead_merchant_labels = {
            row.id: row.name
            for row in merchants
            if row.id in teamlead_merchant_ids
        }
        teamlead_trader_ids = {row.trader_id for row in assignment_rows}
        teamlead_trader_labels = {}
        if teamlead_trader_ids:
            teamlead_trader_labels = dict((await db.execute(
                select(User.id, User.email).where(User.id.in_(teamlead_trader_ids))
            )).all())
        balances_by_teamlead = {}
        accruals_by_teamlead: dict[UUID, list] = {}
        merchant_accruals_by_teamlead: dict[UUID, list] = {}
        settlements_by_teamlead: dict[UUID, list] = {}
        ledger_by_teamlead: dict[UUID, list] = {}
        if user.role == Role.superadmin.value and teamlead_ids:
            balances_by_teamlead = {
                row.teamlead_id: row
                for row in (await db.execute(
                    select(TeamLeadBalance).where(
                        TeamLeadBalance.teamlead_id.in_(teamlead_ids)
                    )
                )).scalars().all()
            }
            for row in (await db.execute(
                select(TeamLeadAccrual)
                .where(TeamLeadAccrual.teamlead_id.in_(teamlead_ids))
                .order_by(TeamLeadAccrual.created_at.desc())
                .limit(1000)
            )).scalars().all():
                accruals_by_teamlead.setdefault(row.teamlead_id, []).append(row)
            for row in (await db.execute(
                select(TeamLeadMerchantAccrual)
                .where(TeamLeadMerchantAccrual.teamlead_id.in_(teamlead_ids))
                .order_by(TeamLeadMerchantAccrual.created_at.desc())
                .limit(1000)
            )).scalars().all():
                merchant_accruals_by_teamlead.setdefault(
                    row.teamlead_id,
                    [],
                ).append(row)
            for row in (await db.execute(
                select(TeamLeadSettlement)
                .where(TeamLeadSettlement.teamlead_id.in_(teamlead_ids))
                .order_by(TeamLeadSettlement.requested_at.desc())
                .limit(1000)
            )).scalars().all():
                settlements_by_teamlead.setdefault(row.teamlead_id, []).append(row)
            for row in (await db.execute(
                select(TeamLeadLedgerEntry)
                .where(TeamLeadLedgerEntry.teamlead_id.in_(teamlead_ids))
                .order_by(TeamLeadLedgerEntry.created_at.desc())
                .limit(2000)
            )).scalars().all():
                ledger_by_teamlead.setdefault(row.teamlead_id, []).append(row)
        for teamlead_user in teamlead_users:
            assignments = [
                row
                for row in assignment_rows
                if row.teamlead_id == teamlead_user.id
            ]
            current_assignments = [
                row for row in assignments if row.effective_to is None
            ]
            merchant_assignments = [
                row
                for row in merchant_assignment_rows
                if row.teamlead_id == teamlead_user.id
            ]
            current_merchant_assignments = [
                row for row in merchant_assignments if row.valid_to is None
            ]
            balance_row = balances_by_teamlead.get(teamlead_user.id)
            reconciliation = None
            if user.role == Role.superadmin.value and balance_row is not None:
                reconciliation = await reconcile_teamlead_account(
                    db, teamlead_user.id
                )
            teamlead_admin_rows.append({
                'user': teamlead_user,
                'assignments': assignments,
                'current_assignments': current_assignments,
                'trader_labels': teamlead_trader_labels,
                'merchant_assignments': merchant_assignments,
                'current_merchant_assignments': current_merchant_assignments,
                'merchant_labels': teamlead_merchant_labels,
                'balance': balance_row,
                'accruals': accruals_by_teamlead.get(teamlead_user.id, []),
                'merchant_accruals': merchant_accruals_by_teamlead.get(
                    teamlead_user.id,
                    [],
                ),
                'settlements': settlements_by_teamlead.get(teamlead_user.id, []),
                'ledger': ledger_by_teamlead.get(teamlead_user.id, []),
                'reconciliation': reconciliation,
                'adjustment_idempotency_key': (
                    f'teamlead-adjust:{teamlead_user.id}:{secrets.token_urlsafe(12)}'
                ),
            })
    if role == Role.merchant.value and merchant:
        all_traders_for_merchant = (await db.execute(select(User).where(User.role.in_([Role.operator.value, Role.trader.value]), User.is_archived.is_(False)).order_by(User.email.asc()).limit(500))).scalars().all()
        merchant_traders = [
            trader for trader in all_traders_for_merchant
            if str(merchant.id) in {str(item) for item in (trader.trader_assigned_merchants or [])}
        ]
    if not users and user.role == Role.support.value:
        users = trader_users
    # Superadmin-only read-only view of existing merchant API credentials.
    # No users, keys or database records are recreated here.
    merchant_credentials = {}
    if user.role in [Role.superadmin.value, Role.admin.value, Role.support.value] and merchants:
        merchant_ids = [m.id for m in merchants]
        keys = (await db.execute(select(ApiKey).where(ApiKey.merchant_id.in_(merchant_ids)).order_by(ApiKey.created_at.desc()))).scalars().all() if user.role in [Role.superadmin.value, Role.admin.value] else []
        webhook_signing_keys = (
            await db.execute(
                select(MerchantWebhookSigningKey)
                .where(
                    MerchantWebhookSigningKey.merchant_id.in_(merchant_ids)
                )
                .order_by(MerchantWebhookSigningKey.created_at.desc())
            )
        ).scalars().all() if user.role == Role.superadmin.value else []
        keys_by_merchant = {}
        for k in keys:
            keys_by_merchant.setdefault(str(k.merchant_id), []).append(k)
        webhook_keys_by_merchant = {}
        for webhook_key in webhook_signing_keys:
            webhook_keys_by_merchant.setdefault(
                str(webhook_key.merchant_id),
                [],
            ).append(webhook_key)
        for m in merchants:
            merchant_keys = keys_by_merchant.get(str(m.id), [])
            merchant_webhook_keys = webhook_keys_by_merchant.get(
                str(m.id),
                [],
            )
            merchant_credentials[str(m.owner_id)] = {
                "merchant_id": str(m.id),
                "name": m.name,
                "webhook_url": m.webhook_url or "",
                "ip_whitelist": ", ".join(m.ip_whitelist or []),
                "sandbox_mode": m.sandbox_mode,
                "commission_percent": m.merchant_commission_percent,
                "keys": merchant_keys,
                "active_key_modes": {k.mode for k in merchant_keys if k.is_active},
                "webhook_signing_keys": merchant_webhook_keys,
                "has_active_webhook_signing_key": any(
                    key.status == 'active'
                    for key in merchant_webhook_keys
                ),
                "webhook_configuration_required": (
                    bool(m.webhook_url)
                    and not any(
                        key.status == 'active'
                        for key in merchant_webhook_keys
                    )
                ),
            }
            if user.role == Role.superadmin.value:
                finance_balance = (await db.execute(
                    select(Balance).where(
                        Balance.merchant_id == m.id,
                        Balance.currency == 'RUB',
                    )
                )).scalar_one_or_none()
                finance_account = await get_rolling_account(db, m.id)
                finance_transfers = await list_rolling_transfers(db, m.id)
                finance_allocations = (await db.execute(
                    select(MerchantRollingAllocation)
                    .where(MerchantRollingAllocation.merchant_id == m.id)
                    .order_by(MerchantRollingAllocation.created_at.desc())
                    .limit(100)
                )).scalars().all()
                finance_ledger = (await db.execute(
                    select(MerchantRollingLedgerEntry)
                    .where(MerchantRollingLedgerEntry.merchant_id == m.id)
                    .order_by(MerchantRollingLedgerEntry.created_at.desc())
                    .limit(200)
                )).scalars().all()
                finance_consumptions = (await db.execute(
                    select(MerchantRollingTransferConsumption)
                    .where(
                        MerchantRollingTransferConsumption.merchant_id
                        == m.id
                    )
                    .order_by(
                        MerchantRollingTransferConsumption.created_at.desc()
                    )
                    .limit(300)
                )).scalars().all()
                merchant_finance_rows[str(m.id)] = {
                    'merchant': m,
                    'balance': finance_balance,
                    'account': finance_account,
                    'overview': await rolling_overview(db, m.id),
                    'transfers': finance_transfers,
                    'allocations': finance_allocations,
                    'ledger': finance_ledger,
                    'consumptions': finance_consumptions,
                    'reconciliation': await reconcile_rolling_account(db, m.id),
                }

    if user.role in [Role.superadmin.value, Role.admin.value, Role.support.value]:
        merchant_settlements = (await db.execute(select(MerchantSettlement).order_by(MerchantSettlement.created_at.desc()).limit(300))).scalars().all()
    elif role == Role.merchant.value and merchant:
        merchant_settlements = (await db.execute(select(MerchantSettlement).where(MerchantSettlement.merchant_id == merchant.id).order_by(MerchantSettlement.created_at.desc()).limit(100))).scalars().all()
    if merchant_settlements:
        settlement_merchants = (await db.execute(select(Merchant).where(Merchant.id.in_({s.merchant_id for s in merchant_settlements})))).scalars().all()
        settlement_merchant_names = {str(m.id): m.name for m in settlement_merchants}
    if user.role in [Role.superadmin.value, Role.admin.value, Role.support.value]:
        webhook_events = (await db.execute(select(WebhookEvent).order_by(WebhookEvent.created_at.desc()).limit(300))).scalars().all()
    elif role == Role.merchant.value and merchant:
        webhook_events = (await db.execute(select(WebhookEvent).where(WebhookEvent.merchant_id == merchant.id).order_by(WebhookEvent.created_at.desc()).limit(300))).scalars().all()
    if webhook_events:
        webhook_event_ids = [event.id for event in webhook_events]
        webhook_attempt_rows = (await db.execute(
            select(WebhookDeliveryAttempt)
            .where(WebhookDeliveryAttempt.webhook_event_id.in_(webhook_event_ids))
            .order_by(
                WebhookDeliveryAttempt.webhook_event_id.asc(),
                WebhookDeliveryAttempt.attempt_no.asc(),
            )
        )).scalars().all()
        attempts_by_event_id = build_attempts_by_event_id(webhook_attempt_rows)
        webhook_event_views = [
            build_webhook_event_view(
                event,
                attempts_by_event_id.get(event.id, []),
            )
            for event in webhook_events
        ]
    if user.role in [Role.superadmin.value, Role.admin.value, Role.support.value]:
        aggregator_accounts = (await db.execute(select(AggregatorAccount).where(AggregatorAccount.is_archived.is_(False)).order_by(AggregatorAccount.created_at.desc()).limit(300))).scalars().all()
        aggregator_name_by_id = {str(account.id): account.name for account in aggregator_accounts}
        aggregator_ids = [account.id for account in aggregator_accounts]
        if aggregator_ids:
            aggregator_payments = (await db.execute(
                select(AggregatorPayment)
                .where(AggregatorPayment.aggregator_id.in_(aggregator_ids))
                .order_by(AggregatorPayment.created_at.desc())
                .limit(300)
            )).scalars().all()
            aggregator_payment_ids = [payment.id for payment in aggregator_payments]
            if aggregator_payment_ids:
                aggregator_callback_logs = (await db.execute(
                    select(AggregatorCallbackLog)
                    .where(AggregatorCallbackLog.related_payment_id.in_(aggregator_payment_ids))
                    .order_by(AggregatorCallbackLog.created_at.desc())
                    .limit(300)
                )).scalars().all()

    if user.role in {Role.superadmin.value, Role.admin.value}:
        all_fee_merchants = (await db.execute(
            select(Merchant).order_by(Merchant.name.asc(), Merchant.id.asc()).limit(1000)
        )).scalars().all()
        all_fee_traders = (await db.execute(
            select(User)
            .where(User.role.in_([Role.trader.value, Role.operator.value]))
            .order_by(User.email.asc(), User.id.asc())
            .limit(1000)
        )).scalars().all()
        all_fee_aggregators = (await db.execute(
            select(AggregatorAccount).order_by(AggregatorAccount.name.asc(), AggregatorAccount.id.asc()).limit(1000)
        )).scalars().all()
        identity_user_ids = {
            *(merchant.owner_id for merchant in all_fee_merchants),
            *(rule.created_by for rule in fee_rules if rule.created_by),
            *(rule.updated_by for rule in fee_rules if rule.updated_by),
        }
        identity_users = (await db.execute(
            select(User).where(User.id.in_(identity_user_ids))
        )).scalars().all() if identity_user_ids else []
        identity_user_by_id = {row.id: row for row in identity_users}
        fee_rule_actor_labels = {
            str(row.id): row.email for row in identity_users
        }

        merchant_options = []
        for entity in all_fee_merchants:
            owner = identity_user_by_id.get(entity.owner_id)
            is_active_entity = bool(
                not entity.is_archived
                and owner
                and owner.role == Role.merchant.value
                and owner.is_active
                and not owner.is_locked
                and not owner.is_archived
            )
            merchant_options.append({
                'type': 'merchant',
                'id': str(entity.id),
                'name': entity.name,
                'identifier': owner.email if owner else str(entity.owner_id),
                'status': 'active' if is_active_entity else 'archived',
                'active': is_active_entity,
                'route_segment': 'merchants',
            })
        trader_options = []
        for entity in all_fee_traders:
            is_active_entity = entity.is_active and not entity.is_locked and not entity.is_archived
            trader_options.append({
                'type': 'trader',
                'id': str(entity.id),
                'name': entity.email,
                'identifier': entity.email,
                'status': 'active' if is_active_entity else 'archived',
                'active': is_active_entity,
                'route_segment': 'traders',
            })
        aggregator_options = []
        for entity in all_fee_aggregators:
            is_active_entity = entity.status == 'active' and not entity.is_archived
            aggregator_options.append({
                'type': 'aggregator',
                'id': str(entity.id),
                'name': entity.name,
                'identifier': entity.status,
                'status': 'active' if is_active_entity else 'archived',
                'active': is_active_entity,
                'route_segment': 'aggregators',
            })

        all_fee_entity_options = [*merchant_options, *trader_options, *aggregator_options]
        fee_entity_options = [option for option in all_fee_entity_options if option['active']]
        fee_entity_by_key = {
            f"{option['type']}:{option['id']}": option
            for option in all_fee_entity_options
        }
        fee_rule_entity_labels = fee_entity_by_key
        income_merchant_options = merchant_options
        income_trader_options = trader_options
        income_aggregator_options = aggregator_options

        selected_fee_key = request.query_params.get('tariff_entity', '').strip()
        selected_fee_entity = next(
            (option for option in fee_entity_options if f"{option['type']}:{option['id']}" == selected_fee_key),
            None,
        )
        if selected_fee_entity:
            selected_fee_rules = [
                rule for rule in fee_rules
                if rule.entity_type == selected_fee_entity['type']
                and str(rule.entity_id) == selected_fee_entity['id']
            ]
    can_view_operations = user.role in [Role.superadmin.value, Role.admin.value, Role.support.value, Role.operator.value, Role.trader.value]
    trader_deposit_view = None
    if trader_subject:
        trader_deposit_view = await _load_trader_deposit_view(
            db,
            subject_trader_id=trader_subject.id,
            filters=deposit_filters,
        )
        requisites = trader_deposit_view['requisites']
    elif can_view_operations:
        requisites = (await db.execute(select(Requisite).where(Requisite.is_archived.is_(False)).order_by(Requisite.created_at.desc()).limit(300))).scalars().all()
    else:
        requisites = []
    trader_requisites = {}
    for r in requisites:
        if r.trader_id:
            trader_requisites.setdefault(str(r.trader_id), []).append(r)
    trader_email_map = {str(t.id): t.email for t in trader_users}
    req_by_id = {r.id: r for r in requisites}
    requisite_values = {
        str(r.id): display_requisite_value(r)
        for r in requisites
        if _can_view_requisite_value(user, r)
    }
    trader_deposit_ids_for_appeals = set()
    if trader_subject:
        deposits = trader_deposit_view['deposits']
        deposit_aggregates = trader_deposit_view['aggregates']
    elif can_view_operations:
        deposits = (await db.execute(select(Deposit).order_by(Deposit.created_at.desc()).limit(500))).scalars().all()
    elif role == Role.merchant.value and merchant:
        deposits = (await db.execute(select(Deposit).where(Deposit.merchant_id == merchant.id).order_by(Deposit.created_at.desc()).limit(500))).scalars().all()
    else:
        deposits = []
    if user.role in [Role.operator.value, Role.trader.value]:
        trader_deposit_ids_for_appeals = {dep.id for dep in deposits}
    if role == Role.merchant.value and merchant:
        merchant_all_deposits = list(deposits)
    deposit_requisite_ids = {d.requisites_id for d in deposits if d.requisites_id and d.requisites_id not in req_by_id}
    if deposit_requisite_ids:
        more_reqs = (await db.execute(select(Requisite).where(Requisite.id.in_(deposit_requisite_ids)))).scalars().all()
        for req in more_reqs:
            req_by_id[req.id] = req
            if req.trader_id:
                trader_requisites.setdefault(str(req.trader_id), []).append(req)
            merchant_related = bool(role == Role.merchant.value and merchant and any(d.requisites_id == req.id for d in merchant_all_deposits))
            if _can_view_requisite_value(user, req, merchant_related=merchant_related):
                requisite_values[str(req.id)] = display_requisite_value(req)
    if role == Role.merchant.value and merchant:
        filtered_merchant_deposits = list(merchant_all_deposits)
        if deposit_filters['query']:
            needle = deposit_filters['query'].lower()
            filtered_merchant_deposits = [
                dep for dep in filtered_merchant_deposits
                if needle in str(dep.id).lower() or needle in str(dep.external_id or '').lower()
            ]
        if merchant_filter_trader_id:
            filtered_merchant_deposits = [
                dep for dep in filtered_merchant_deposits
                if dep.requisites_id and req_by_id.get(dep.requisites_id) and str(req_by_id[dep.requisites_id].trader_id) == merchant_filter_trader_id
            ]
        deposits = filtered_merchant_deposits
    if (
        any(deposit_filters.values())
        and not trader_subject
        and user.role not in [Role.operator.value, Role.trader.value]
    ):
        deposits = [
            dep for dep in deposits
            if deposit_matches_filters(dep, req_by_id.get(dep.requisites_id), deposit_filters)
        ]
    if trader_subject:
        deposit_details = _trader_deposit_details(
            user,
            trader_subject,
            deposits,
            requisites,
        )
    else:
        now = datetime.now(timezone.utc)
        deposit_details = {}
        for dep in deposits:
            req = req_by_id.get(dep.requisites_id)
            deadline = _deposit_deadline(dep)
            remaining_seconds = max(0, int((deadline - now).total_seconds()))
            merchant_related = bool(role == Role.merchant.value and merchant and dep.merchant_id == merchant.id)
            deposit_details[str(dep.id)] = {
                'requisite': display_requisite_value(req) if req and _can_view_requisite_value(user, req, merchant_related=merchant_related) else '',
                'bank': requisite_provider_display(req.method, req.bank_code, req.operator_code, req.bank_name) if req else '',
                'provider_label': requisite_provider_label(req.method) if req else 'Банк',
                'provider_name': requisite_provider_display(req.method, req.bank_code, req.operator_code, req.bank_name) if req else '',
                'receiver_name': (req.full_name or req.owner_name) if req else '',
                'deadline_iso': deadline.isoformat(),
                'remaining_seconds': remaining_seconds,
                'is_active': dep.status in DEPOSIT_ACTIVE_FOR_TRADER,
                'merchant_fee': (dep.metadata_json or {}).get('merchant_fee_amount', ''),
                'merchant_payable': (
                    (dep.metadata_json or {}).get('merchant_payable_amount')
                    or (dep.metadata_json or {}).get(
                        'merchant_' 'net_amount',
                        '',
                    )
                ),
                'platform_income': (dep.metadata_json or {}).get('platform_income_amount', ''),
                'failure_reason': (dep.metadata_json or {}).get('failure_reason', ''),
            }
    if user.role in [Role.superadmin.value, Role.admin.value, Role.support.value]:
        payouts = (await db.execute(select(Payout).order_by(Payout.created_at.desc()).limit(200))).scalars().all()
    elif user.role in [Role.operator.value, Role.trader.value]:
        payouts = []
    elif role == Role.merchant.value and merchant:
        payouts = (await db.execute(select(Payout).where(Payout.merchant_id == merchant.id).order_by(Payout.created_at.desc()).limit(200))).scalars().all()
    else:
        payouts = []
    if user.role in [Role.superadmin.value, Role.admin.value, Role.support.value]:
        appeals = (await db.execute(select(Appeal).order_by(Appeal.created_at.desc()).limit(500))).scalars().all()
    elif role == Role.merchant.value and merchant and merchant_all_deposits:
        merchant_deposit_ids = {dep.id for dep in merchant_all_deposits}
        appeals = (await db.execute(select(Appeal).where(Appeal.operation_id.in_(merchant_deposit_ids)).order_by(Appeal.created_at.desc()).limit(500))).scalars().all()
    elif user.role in [Role.operator.value, Role.trader.value] and trader_deposit_ids_for_appeals:
        appeals = (await db.execute(
            select(Appeal)
            .where(
                Appeal.operation_type == 'deposit',
                Appeal.operation_id.in_(trader_deposit_ids_for_appeals),
            )
            .order_by(Appeal.created_at.desc())
            .limit(500)
        )).scalars().all()
    else:
        appeals = []
    audit_logs = (await db.execute(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(800))).scalars().all() if user.role == Role.superadmin.value else []
    audit_actor_names = {}
    audit_actor_ids = {log.actor_id for log in audit_logs if log.actor_id}
    if audit_actor_ids:
        audit_actor_users = (await db.execute(select(User).where(User.id.in_(audit_actor_ids)))).scalars().all()
        audit_actor_names = {
            str(actor.id): f"{actor.email} ({'trader' if actor.role in [Role.operator.value, Role.trader.value] else actor.role})"
            for actor in audit_actor_users
        }
    audit_views = [
        build_audit_view(
            row,
            audit_actor_names.get(
                str(row.actor_id or ''),
                'Система' if not row.actor_id else str(row.actor_id),
            ),
        )
        for row in audit_logs
    ]
    if user.role == Role.superadmin.value:
        for rolling_merchant in merchants:
            finance_row = merchant_finance_rows[str(rolling_merchant.id)]
            account = finance_row['account']
            overview = finance_row['overview']
            allocations = finance_row['allocations']
            ledger_rows = finance_row['ledger']
            reconciliation = finance_row['reconciliation']
            rolling_admin_rows.append({
                'merchant': rolling_merchant,
                'account': account,
                'overview': overview,
                'transfers': finance_row['transfers'],
                'allocations': allocations,
                'ledger': ledger_rows,
                'settle_overflow_rub': sum(
                    (
                        Decimal(row.amount_rub or 0)
                        for row in ledger_rows
                        if row.entry_type == 'settle_overflow'
                    ),
                    Decimal('0.00'),
                ),
                'settle_balance_rub': (
                    finance_row['balance'].available
                    if finance_row['balance']
                    else Decimal('0.00')
                ),
                'reconciliation': reconciliation,
                'audit': [
                    row for row in audit_logs
                    if str(row.target_id or '') == str(rolling_merchant.id)
                    or (
                        str(row.target_type or '') == 'deposit'
                        and any(
                            str(allocation.deposit_id) == str(row.target_id or '')
                            for allocation in allocations
                        )
                    )
                ][:100],
            })

    known_traders = {str(t.id): t for t in trader_users}
    for merchant_trader in merchant_traders:
        known_traders[str(merchant_trader.id)] = merchant_trader
    if user.role in [Role.operator.value, Role.trader.value]:
        known_traders[str(user.id)] = user
    extra_trader_ids = {req.trader_id for req in req_by_id.values() if req.trader_id and str(req.trader_id) not in known_traders}
    if extra_trader_ids:
        extra_traders = (await db.execute(select(User).where(User.id.in_(extra_trader_ids)))).scalars().all()
        for extra_trader in extra_traders:
            known_traders[str(extra_trader.id)] = extra_trader
            if role == Role.merchant.value and all(str(existing.id) != str(extra_trader.id) for existing in merchant_traders):
                merchant_traders.append(extra_trader)

    for dep in deposits:
        req = req_by_id.get(dep.requisites_id)
        trader_for_dep = known_traders.get(str(req.trader_id)) if req and req.trader_id else None
        if str(dep.id) in deposit_details:
            deposit_details[str(dep.id)]['trader_profit'] = str(deposit_trader_profit_amount(dep, trader_for_dep))
            deposit_details[str(dep.id)]['trader_settlement'] = str(deposit_trader_settlement_amount(dep, trader_for_dep))
            deposit_details[str(dep.id)]['trader_email'] = trader_for_dep.email if trader_for_dep else ''


    appeal_messages_by_id = {}
    appeal_details = {}
    appeal_deposit_ids = {a.operation_id for a in appeals if a.operation_type == 'deposit'}
    appeal_deposits = {}
    if appeal_deposit_ids:
        appeal_deposit_rows = (await db.execute(select(Deposit).where(Deposit.id.in_(appeal_deposit_ids)))).scalars().all()
        appeal_deposits = {dep.id: dep for dep in appeal_deposit_rows}
        missing_req_ids = {dep.requisites_id for dep in appeal_deposit_rows if dep.requisites_id and dep.requisites_id not in req_by_id}
        if missing_req_ids:
            appeal_reqs = (await db.execute(select(Requisite).where(Requisite.id.in_(missing_req_ids)))).scalars().all()
            for req in appeal_reqs:
                req_by_id[req.id] = req
                if req.trader_id:
                    trader_requisites.setdefault(str(req.trader_id), []).append(req)
                    if str(req.trader_id) not in known_traders:
                        trader_user = (await db.execute(select(User).where(User.id == req.trader_id))).scalar_one_or_none()
                        if trader_user:
                            known_traders[str(trader_user.id)] = trader_user
    if appeals:
        appeal_ids = [a.id for a in appeals]
        appeal_messages = (await db.execute(select(AppealMessage).where(AppealMessage.appeal_id.in_(appeal_ids)).order_by(AppealMessage.created_at.asc()))).scalars().all()
        author_ids = {m.author_id for m in appeal_messages if m.author_id}
        author_names = {}
        if author_ids:
            authors = (await db.execute(select(User).where(User.id.in_(author_ids)))).scalars().all()
            author_names = {str(a.id): a.email for a in authors}
        for msg in appeal_messages:
            appeal_messages_by_id.setdefault(str(msg.appeal_id), []).append({
                'author': author_names.get(str(msg.author_id), 'Система') if msg.author_id else 'Система',
                'message': msg.message,
                'attachment_path': msg.attachment_path,
                'created_at': msg.created_at,
            })
    appeal_merchant_ids = {dep.merchant_id for dep in appeal_deposits.values()}
    appeal_merchant_names = {}
    if appeal_merchant_ids:
        appeal_merchants = (await db.execute(select(Merchant).where(Merchant.id.in_(appeal_merchant_ids)))).scalars().all()
        appeal_merchant_names = {str(m.id): m.name for m in appeal_merchants}
    for appeal in appeals:
        dep = appeal_deposits.get(appeal.operation_id)
        req = req_by_id.get(dep.requisites_id) if dep and dep.requisites_id else None
        meta = appeal_metadata(appeal)
        trader_id = str(meta.get('trader_id') or (req.trader_id if req and req.trader_id else ''))
        trader = known_traders.get(trader_id)
        remaining = appeal_remaining_seconds(appeal)
        appeal_details[str(appeal.id)] = {
            'deposit': dep,
            'merchant_name': appeal_merchant_names.get(str(dep.merchant_id), str(dep.merchant_id)) if dep else '?',
            'trader_id': trader_id,
            'trader_email': trader.email if trader else trader_email_map.get(trader_id, '?'),
            'amount_claimed': meta.get('amount_claimed') or (str(dep.amount) if dep else '0.00'),
            'requisite': meta.get('requisite') or (display_requisite_value(req) if req else ''),
            'recipient_bank': meta.get('recipient_bank') or (requisite_provider_display(req.method, req.bank_code, req.operator_code, req.bank_name) if req else ''),
            'operation_date': meta.get('operation_date') or (dep.created_at if dep else ''),
            'receipt_url': _appeal_file_info_url(meta.get('receipt_file'), cabinet_base),
            'receipt_name': (meta.get('receipt_file') or {}).get('original_name', 'чек'),
            'statement_url': _appeal_file_info_url(meta.get('statement_file'), cabinet_base),
            'statement_name': (meta.get('statement_file') or {}).get('original_name', 'выписка'),
            'deadline_iso': appeal_deadline(appeal).isoformat(),
            'remaining_seconds': remaining,
            'is_timer_active': remaining > 0 and appeal.status == AppealStatus.opened.value and not meta.get('trader_decision'),
            'trader_rejection_reason_label': meta.get('trader_rejection_reason_label', ''),
            'corrected_amount': meta.get('corrected_amount', ''),
            'previous_deposit_status': meta.get('previous_deposit_status', ''),
            'can_trader_act': user.role in [Role.operator.value, Role.trader.value] and trader_id == str(user.id) and appeal.status == AppealStatus.opened.value,
            'can_staff_extend': user.role in [Role.superadmin.value, Role.admin.value, Role.support.value] and appeal.status in [AppealStatus.opened.value, AppealStatus.in_review.value],
            'can_staff_final': user.role in [Role.superadmin.value, Role.admin.value] and appeal.status in [AppealStatus.opened.value, AppealStatus.in_review.value],
        }
    display_appeals = [
        appeal for appeal in appeals
        if appeal_matches_filters(
            appeal,
            appeal_details.get(str(appeal.id), {}).get('deposit'),
            appeal_details.get(str(appeal.id), {}),
            appeal_filters,
        )
    ] if any(appeal_filters.values()) else list(appeals)

    trader_stats = {}
    trader_details = {}
    active_statuses = {'created', 'pending', 'processing', 'appeal_opened'}
    success_statuses = {'paid', 'completed'}
    failed_statuses = {DepositStatus.failed.value, DepositStatus.cancelled.value, DepositStatus.expired.value}

    def conversion(success_count: int, total_count: int) -> str:
        return f'{round((success_count / total_count) * 100, 2)}%' if total_count else '0%'

    def money_from_metadata(dep: Deposit, key: str) -> Decimal:
        return safe_decimal(str((dep.metadata_json or {}).get(key, '0')), '0')

    def merchant_payable_from_metadata(dep: Deposit) -> Decimal:
        metadata = dep.metadata_json or {}
        value = metadata.get('merchant_payable_amount')
        if value is None:
            # Read-only compatibility for immutable pre-0020 deposit metadata.
            value = metadata.get('merchant_' 'net_amount', '0')
        return safe_decimal(str(value), '0')

    if role == Role.merchant.value and merchant:
        merchant_stat_deposits = list(merchant_all_deposits)
        merchant_successful = [dep for dep in merchant_stat_deposits if dep.status in success_statuses]
        merchant_failed = [dep for dep in merchant_stat_deposits if dep.status in failed_statuses]
        merchant_active = [dep for dep in merchant_stat_deposits if dep.status in active_statuses]
        merchant_total_count = len(merchant_stat_deposits)
        total_turnover = sum((dep.amount for dep in merchant_successful), Decimal('0.00'))
        merchant_payable = sum(
            (merchant_payable_from_metadata(dep) for dep in merchant_successful),
            Decimal('0.00'),
        )
        merchant_fee = sum((money_from_metadata(dep, 'merchant_fee_amount') for dep in merchant_successful), Decimal('0.00'))
        platform_income = sum((money_from_metadata(dep, 'platform_income_amount') for dep in merchant_successful), Decimal('0.00'))
        trader_candidates = {str(trader.id): trader for trader in merchant_traders}
        for dep in merchant_stat_deposits:
            req = req_by_id.get(dep.requisites_id)
            if req and req.trader_id and str(req.trader_id) in known_traders:
                trader_candidates[str(req.trader_id)] = known_traders[str(req.trader_id)]
        for trader in trader_candidates.values():
            related_deposits = [
                dep for dep in merchant_stat_deposits
                if dep.requisites_id and req_by_id.get(dep.requisites_id) and str(req_by_id[dep.requisites_id].trader_id) == str(trader.id)
            ]
            related_successful = [dep for dep in related_deposits if dep.status in success_statuses]
            related_failed = [dep for dep in related_deposits if dep.status in failed_statuses]
            related_active = [dep for dep in related_deposits if dep.status in active_statuses]
            related_ids = {dep.id for dep in related_deposits}
            related_appeals = [appeal for appeal in appeals if appeal.operation_id in related_ids]
            merchant_trader_rows.append({
                'email': trader.email,
                'turnover': sum((dep.amount for dep in related_successful), Decimal('0.00')),
                'successful_count': len(related_successful),
                'failed_count': len(related_failed),
                'active_count': len(related_active),
                'total_count': len(related_deposits),
                'conversion': conversion(len(related_successful), len(related_deposits)),
                'appeals': len(related_appeals),
            })
        merchant_trader_rows.sort(key=lambda item: item['turnover'], reverse=True)
        merchant_stats = {
            'total_turnover': total_turnover,
            'merchant_payable': merchant_payable,
            'merchant_fee': merchant_fee,
            'platform_income': platform_income,
            'successful_count': len(merchant_successful),
            'failed_count': len(merchant_failed),
            'active_count': len(merchant_active),
            'total_count': merchant_total_count,
            'conversion': conversion(len(merchant_successful), merchant_total_count),
            'appeals_count': len(appeals),
            'traders_count': len(merchant_trader_rows),
        }
    def build_trader_stats(t: User) -> dict:
        req_ids = {r.id for r in trader_requisites.get(str(t.id), [])}
        related_deposits = [d for d in deposits if d.requisites_id in req_ids]
        related_deposit_ids = {d.id for d in related_deposits}
        related_appeals = [a for a in appeals if a.operation_id in related_deposit_ids]
        active_requests = sum(1 for d in related_deposits if d.status in active_statuses)
        successful_deposits = [d for d in related_deposits if d.status in success_statuses]
        failed_deposits = [d for d in related_deposits if d.status in {DepositStatus.failed.value, DepositStatus.cancelled.value, DepositStatus.expired.value}]
        turnover = sum((d.amount for d in successful_deposits), Decimal('0.00'))
        profit = sum((deposit_trader_profit_amount(d, t) for d in successful_deposits), Decimal('0.00'))
        settlement = sum((deposit_trader_settlement_amount(d, t) for d in successful_deposits), Decimal('0.00'))
        total = len(related_deposits)
        conversion_value = conversion(len(successful_deposits), total)
        return {
            'active_requests': active_requests,
            'appeals': len(related_appeals),
            'conversion': conversion_value,
            'turnover': turnover,
            'profit': profit,
            'settlement': settlement,
            'successful_count': len(successful_deposits),
            'failed_count': len(failed_deposits),
            'total_count': total,
            'available': max(Decimal('0.00'), safe_decimal(str(t.trader_balance), '0') - safe_decimal(str(t.trader_hold), '0')),
        }

    for t in trader_users:
        stats = build_trader_stats(t)
        trader_stats[str(t.id)] = stats
        req_ids = {r.id for r in trader_requisites.get(str(t.id), [])}
        related_deposits = [d for d in deposits if d.requisites_id in req_ids]
        related_deposit_ids = {d.id for d in related_deposits}
        related_appeals = [a for a in appeals if a.operation_id in related_deposit_ids]
        related_audits = [a for a in audit_logs if str(a.actor_id or '') == str(t.id) or str(a.target_id or '') == str(t.id)]
        trader_details[str(t.id)] = {
            'stats': stats,
            'deposits': related_deposits[:80],
            'appeals': related_appeals[:80],
            'audit_logs': related_audits[:80],
            'requisites': trader_requisites.get(str(t.id), []),
        }

    current_trader_stats = build_trader_stats(trader_subject) if trader_subject else {}
    if user.role == Role.superadmin.value:
        antiscam = await antiscam_dashboard_rows(db)
        filtered_risk_requisites = list(antiscam.get('requisites', []))
        if risk_filters['query']:
            needle = risk_filters['query'].casefold()
            filtered_risk_requisites = [
                row for row in filtered_risk_requisites
                if needle in str(requisite_values.get(str(row.id), '')).casefold()
                or needle in str(row.id).casefold()
            ]
        if risk_filters['trader_id']:
            filtered_risk_requisites = [
                row for row in filtered_risk_requisites
                if str(row.trader_id or '') == risk_filters['trader_id']
            ]
        if risk_filters['bank_code']:
            filtered_risk_requisites = [
                row for row in filtered_risk_requisites
                if row.bank_code == risk_filters['bank_code']
            ]
        if risk_filters['payment_method']:
            filtered_risk_requisites = [
                row for row in filtered_risk_requisites
                if row.method == risk_filters['payment_method']
            ]
        if risk_filters['state']:
            filtered_risk_requisites = [
                row for row in filtered_risk_requisites
                if row.traffic_status == risk_filters['state']
            ]
        antiscam['requisites'] = filtered_risk_requisites
    rapira_quote = await get_rapira_rub_usdt_quote()
    rapira_source = f'{rapira_quote.source} stale' if rapira_quote.stale else rapira_quote.source
    bank_options = [
        {
            'code': bank.code,
            'display_name': bank.display_name,
            'aliases': list(bank.aliases),
        }
        for bank in get_enabled_banks()
    ]
    mobile_operator_options = [
        {
            'code': operator.code,
            'display_name': operator.display_name,
            'aliases': list(operator.aliases),
        }
        for operator in get_enabled_mobile_operators()
    ]
    context = {
        'request': request,
        'ui_message': ui_message,
        'session_view_token': session_view_fingerprint(request.session),
        'cabinet_poll_interval_seconds': settings.CABINET_POLL_INTERVAL_SECONDS,
        'auth_realm': auth_realm,
        'cabinet_base': cabinet_base,
        'logout_path': f'/{auth_realm}/logout' if auth_realm else '/logout',
        'login_path': realm_login_path(auth_realm) if auth_realm else '/login',
        **branding_context(),
        'user': user,
        'role': role,
        'title': ROLE_TITLES.get(role, role),
        'description': ROLE_DESCRIPTIONS.get(role, ''),
        'sections': sections,
        'merchant': merchant,
        'merchant_api_keys': merchant_api_keys,
        'environment_name': settings.ENV,
        'app_version': settings.APP_VERSION,
        'is_production_environment': settings.is_production,
        'allow_sandbox_keys_in_production': settings.ALLOW_SANDBOX_KEYS_IN_PRODUCTION,
        'merchant_api_base_url': merchant_api_base_url,
        'merchant_local_docker_api_base_url': merchant_local_docker_api_base_url,
        'balance': balance,
        'users': users,
        'merchant_users': merchant_users,
        'merchant_credentials': merchant_credentials,
        'merchant_settlements': merchant_settlements,
        'settlement_merchant_names': settlement_merchant_names,
        'settlement_quote': settlement_quote,
        'merchant_settlement_idempotency_key': (
            merchant_settlement_idempotency_key
        ),
        'merchant_rolling_overview': merchant_rolling_overview,
        'merchant_rolling_allocations': merchant_rolling_allocations,
        'merchant_rolling_ledger': merchant_rolling_ledger,
        'merchant_rolling_transfers': merchant_rolling_transfers,
        'merchant_rolling_consumptions': merchant_rolling_consumptions,
        'rolling_admin_rows': rolling_admin_rows,
        'merchant_finance_rows': merchant_finance_rows,
        'teamlead_admin_rows': teamlead_admin_rows,
        'merchant_teamlead_assignments': merchant_teamlead_assignments,
        'teamlead_user_labels': teamlead_user_labels,
        'platform_wallet': platform_wallet,
        'platform_wallet_history': platform_wallet_history,
        'platform_wallet_actor_labels': platform_wallet_actor_labels,
        'platform_wallet_qr_url': platform_wallet_qr_url,
        'ai_office_config': ai_office_config,
        'ai_office_event_options': sorted(AI_OFFICE_EVENT_OPTIONS),
        'merchant_traders': merchant_traders,
        'merchant_filter_trader_id': merchant_filter_trader_id,
        'merchant_deal_id': merchant_deal_id,
        'deposit_filters': deposit_filters,
        'deposit_aggregates': deposit_aggregates,
        'deposit_refresh_url': (
            f'{cabinet_base}/partials/deposits?preview=trader'
            if trader_preview_active
            else f'{cabinet_base}/partials/deposits'
        ),
        'deposit_active_statuses': sorted(DEPOSIT_ACTIVE_STATUSES),
        'deposit_terminal_statuses': sorted(DEPOSIT_TERMINAL_STATUSES),
        'appeal_filters': appeal_filters,
        'merchant_stats': merchant_stats,
        'merchant_trader_rows': merchant_trader_rows,
        'fee_rules': fee_rules,
        'fee_entity_options': fee_entity_options,
        'fee_rule_entity_labels': fee_rule_entity_labels,
        'fee_rule_actor_labels': fee_rule_actor_labels,
        'selected_fee_entity': selected_fee_entity,
        'selected_fee_rules': selected_fee_rules,
        'commission_tier_ranges': COMMISSION_TIER_RANGES,
        'commission_tiers_by_entity': commission_tiers_by_entity,
        'income_merchant_options': income_merchant_options,
        'income_trader_options': income_trader_options,
        'income_aggregator_options': income_aggregator_options,
        'income_dashboard': income_dashboard,
        'trader_users': trader_users,
        'trader_preview_active': trader_preview_active,
        'trader_preview_subject': trader_subject,
        'trader_display_user': trader_subject or user,
        'trader_preview_urls': trader_preview_urls,
        'merchants': merchants,
        'trader_requisites': trader_requisites,
        'trader_email_map': trader_email_map,
        'trader_stats': trader_stats,
        'trader_details': trader_details,
        'current_trader_stats': current_trader_stats,
        'antiscam': antiscam,
        'risk_filters': risk_filters,
        'audit_logs': audit_logs,
        'audit_views': audit_views,
        'audit_actor_names': audit_actor_names,
        'rf_banks': RF_BANKS,
        'bank_options': bank_options,
        'mobile_operator_options': mobile_operator_options,
        'provider_display': requisite_provider_display,
        'provider_label': requisite_provider_label,
        'payment_method_options': PAYMENT_METHOD_OPTIONS,
        'payment_method_placeholders': PAYMENT_METHOD_REQUISITE_PLACEHOLDERS,
        'rapira_rate': {
            'rate_rub': rapira_quote.rate_rub,
            'rate_source': rapira_source,
            'updated_at': rapira_quote.updated_at,
            'stale': rapira_quote.stale,
            'cache_seconds': rapira_quote.cache_seconds,
        },
        'requisites': requisites,
        'requisite_values': requisite_values,
        'deposits': deposits,
        'deposit_details': deposit_details,
        'appeals': display_appeals,
        'appeal_details': appeal_details,
        'appeal_messages_by_id': appeal_messages_by_id,
        'appeal_rejection_reasons': TRADER_REJECTION_REASONS,
        'webhook_events': webhook_events,
        'attempts_by_event_id': attempts_by_event_id,
        'webhook_event_views': webhook_event_views,
        'aggregator_accounts': aggregator_accounts,
        'aggregator_payments': aggregator_payments,
        'aggregator_callback_logs': aggregator_callback_logs,
        'aggregator_name_by_id': aggregator_name_by_id,
        'aggregator_secret_flash': aggregator_secret_flash,
        'payouts': payouts,
        'security_qr': totp_qr_data_uri(user.email, decrypt_secret(user.twofa_secret)) if user.twofa_secret else None,
    }
    return templates.TemplateResponse(
        request=request,
        name='cabinet.html',
        context=context,
    )
