"""Independent Sandbox/Production databases; explicit, audited live access.

No operation or balance is ever reclassified. Financial formulas are unchanged.
"""
from datetime import datetime, timezone
from urllib.parse import urlsplit
from sqlalchemy import select, or_
from app.core.config import settings
from app.core.validators import validate_public_webhook_url
from app import models as m
from app.services.audit import audit


class IntegrationModeError(ValueError):
    def __init__(self, code, message, status=409, reasons=None):
        super().__init__(message)
        self.code, self.status, self.reasons = code, status, reasons or []


async def database_mode(db):
    row = await db.get(m.IntegrationEnvironment, 1)
    if not row:
        raise IntegrationModeError('integration_environment_unbound', 'Режим базы данных не настроен.', 503)
    return row.mode


async def verify_environment(db):
    mode = await database_mode(db)
    expected = 'production' if settings.is_production else 'sandbox'
    if mode != expected:
        raise IntegrationModeError('integration_environment_mismatch',
            'Режим сервера не совпадает с режимом базы данных. Подключите отдельное окружение.', 503)
    return mode


async def integration_status(db, merchant):
    mode = await database_mode(db)
    access = await db.get(m.MerchantProductionAccess, merchant.id)
    reasons = []
    def block(code, message): reasons.append({'code': code, 'message': message})
    if mode != 'production' or not settings.is_production:
        block('production_environment_required', 'Это Sandbox. Для Production нужен отдельный сервер с новой чистой БД и отдельными ключами. Тестовые данные не переносятся.')
    if not settings.PRODUCTION_ACTIVATION_ENABLED:
        block('owner_activation_disabled', 'Владелец ещё не разрешил активацию Production в настройках сервера.')
    owner = await db.get(m.User, merchant.owner_id)
    if merchant.is_archived or not owner or not owner.is_active or owner.is_locked or owner.is_archived:
        block('merchant_unavailable', 'Учётная запись мерчанта должна быть активна и доступна.')
    try:
        url = urlsplit(merchant.webhook_url or '')
        if url.scheme != 'https' or not url.hostname or url.username or url.password: raise ValueError()
    except ValueError:
        block('https_webhook_required', 'Укажите публичный HTTPS-адрес Webhook.')
    signing = await db.scalar(select(m.MerchantWebhookSigningKey.id).where(
        m.MerchantWebhookSigningKey.merchant_id == merchant.id, m.MerchantWebhookSigningKey.status == 'active').limit(1))
    if not signing: block('webhook_key_required', 'Выпустите отдельный активный ключ подписи Webhook в этом окружении.')
    key = await db.scalar(select(m.ApiKey.id).where(m.ApiKey.merchant_id == merchant.id,
        m.ApiKey.mode == 'production', m.ApiKey.is_active.is_(True)).limit(1))
    if not key: block('production_key_required', 'Выпустите отдельный Production API-ключ и передайте секрет мерчанту защищённым способом.')
    now = datetime.now(timezone.utc)
    fee = await db.scalar(select(m.FeeRule.id).where(m.FeeRule.entity_type == 'merchant',
        m.FeeRule.entity_id == merchant.id, m.FeeRule.fee_side == 'merchant_fee', m.FeeRule.is_active.is_(True),
        m.FeeRule.effective_from <= now, or_(m.FeeRule.effective_to.is_(None), m.FeeRule.effective_to > now)).limit(1))
    if not fee: block('merchant_fee_required', 'Настройте действующий тариф мерчанта перед активацией.')
    status = access.status if access else 'not_activated'
    label = 'Sandbox' if mode == 'sandbox' else ('Production · активен' if status == 'active' else 'Production · приостановлен' if status == 'suspended' else 'Production · не активирован')
    return {'environment': mode, 'status': status, 'label': label, 'can_activate': not reasons and status != 'active',
            'can_suspend': mode == 'production' and status == 'active', 'blocking_reasons': reasons,
            'activation_role': 'superadmin', 'credentials_are_separate': True}


async def change_production_access(db, merchant_id, actor, *, action, confirmation, reason, ip=None):
    if actor.role != 'superadmin' or not actor.twofa_enabled or not actor.is_active or actor.is_locked or actor.is_archived:
        raise IntegrationModeError('production_activation_forbidden', 'Требуется Superadmin с подтверждённой 2FA.', 403)
    if action not in {'activate','suspend'}:
        raise IntegrationModeError('integration_action_invalid', 'Неизвестное действие.', 422)
    expected = 'PRODUCTION' if action == 'activate' else 'SUSPEND'
    if confirmation != expected or not 10 <= len(reason.strip()) <= 1000:
        raise IntegrationModeError('production_confirmation_required', 'Введите подтверждение и причину (10–1000 символов).', 422)
    merchant = await db.scalar(select(m.Merchant).where(m.Merchant.id == merchant_id).with_for_update())
    if not merchant: raise IntegrationModeError('merchant_not_found', 'Мерчант не найден.', 404)
    await verify_environment(db)
    state = await integration_status(db, merchant)
    access = await db.get(m.MerchantProductionAccess, merchant.id)
    target = 'active' if action == 'activate' else 'suspended'
    if access and access.status == target:
        return state
    reasons = state['blocking_reasons'] if action == 'activate' else []
    if action == 'activate' and not reasons:
        try:
            validate_public_webhook_url(merchant.webhook_url)
        except ValueError:
            reasons.append({'code': 'webhook_not_public', 'message': 'Webhook должен иметь доступный публичный HTTPS-адрес.'})
    if state['environment'] != 'production' or reasons:
        raise IntegrationModeError('production_activation_blocked', 'Production пока недоступен.', reasons=reasons)
    if action == 'suspend' and not access:
        raise IntegrationModeError('production_not_activated', 'Production ещё не был активирован.')
    previous = access.status if access else 'not_activated'
    if not access:
        access = m.MerchantProductionAccess(merchant_id=merchant.id)
        db.add(access)
    access.status, access.changed_by, access.reason = target, actor.id, reason.strip()
    merchant.sandbox_mode = False  # Compatibility projection, not authorization.
    await audit(db, 'merchant_production_' + action, 'merchant', actor.id, merchant.id, ip,
                {'before': previous, 'after': target, 'environment': 'production', 'reason': reason.strip()})
    await db.flush()
    return await integration_status(db, merchant)


async def authorize_api_mode(db, merchant, key_mode):
    mode = await verify_environment(db)
    if key_mode != mode:
        raise IntegrationModeError('api_key_environment_mismatch',
            'API-ключ предназначен для другого окружения. Sandbox и Production используют разные адреса и базы данных.', 403)
    if mode == 'production':
        # Admission uses the latest committed approval (READ COMMITTED). Do not
        # introduce merchant-row locks into the existing financial lock order.
        # Requests admitted before suspension may finish their normal lifecycle.
        access = await db.get(m.MerchantProductionAccess, merchant.id, populate_existing=True)
        if not access or access.status != 'active':
            raise IntegrationModeError('production_not_active', 'Доступ к Production не активирован или приостановлен.', 403)
