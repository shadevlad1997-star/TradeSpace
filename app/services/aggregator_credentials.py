"""Aggregator-owned credentials/admission. Never promotes balances or operations.

Account.secret_hash remains the current outbound callback signing projection.
Rotation replaces it, preserving the established callback signature contract.
"""
import secrets
from datetime import datetime, timezone
from urllib.parse import urlsplit
from sqlalchemy import select, or_
from app import models as m
from app.core.config import settings
from app.core.security import encrypt_secret
from app.core.validators import validate_public_webhook_url
from app.services.audit import audit
from app.services.integration_modes import IntegrationModeError, database_mode, verify_environment


def require_root(actor):
    if actor.role != 'superadmin' or not actor.twofa_enabled or not actor.is_active or actor.is_locked or actor.is_archived:
        raise IntegrationModeError('aggregator_management_forbidden', 'Требуется Superadmin с подтверждённой 2FA.', 403)


def confirm(confirmation, expected, reason):
    if confirmation != expected or not 10 <= len(reason.strip()) <= 1000:
        raise IntegrationModeError('aggregator_confirmation_required', 'Введите подтверждение и причину (10–1000 символов).', 422)


async def locked_account(db, identifier):
    row = await db.scalar(select(m.AggregatorAccount).where(m.AggregatorAccount.id == identifier).with_for_update())
    if not row: raise IntegrationModeError('aggregator_not_found', 'Агрегатор не найден.', 404)
    return row


async def aggregator_integration_status(db, account):
    mode = await database_mode(db)
    access = await db.get(m.AggregatorProductionAccess, account.id)
    keys = (await db.scalars(select(m.AggregatorApiKey).where(m.AggregatorApiKey.aggregator_id == account.id)
                            .order_by(m.AggregatorApiKey.created_at.desc(), m.AggregatorApiKey.id))).all()
    reasons = []
    def block(code, message): reasons.append({'code':code, 'message':message})
    if mode != 'production' or not settings.is_production:
        block('production_environment_required', 'Это Sandbox. Production подключается на отдельном сервере с чистой БД и отдельными ключами.')
    if not settings.PRODUCTION_ACTIVATION_ENABLED:
        block('owner_activation_disabled', 'Владелец ещё не разрешил выпуск ключей и активацию Production.')
    merchant = await db.get(m.Merchant, account.platform_merchant_id)
    if account.status != 'active' or account.is_archived or not merchant or merchant.is_archived:
        block('aggregator_unavailable', 'Агрегатор и связанный расчётный аккаунт должны быть доступны.')
    if not any(k.mode == 'production' and k.status == 'active' for k in keys):
        block('production_key_required', 'Выпустите Production-ключ в этом Production-окружении.')
    if account.callback_url:
        try:
            url = urlsplit(account.callback_url)
            if url.scheme != 'https' or not url.hostname or url.username or url.password: raise ValueError()
        except ValueError:
            block('https_callback_required', 'Адрес callback должен использовать публичный HTTPS.')
    now = datetime.now(timezone.utc)
    for entity, identifier, side in [('merchant',account.platform_merchant_id,'merchant_fee'),('aggregator',account.id,'executor_fee')]:
        rule = await db.scalar(select(m.FeeRule.id).where(m.FeeRule.entity_type == entity,
            m.FeeRule.entity_id == identifier, m.FeeRule.fee_side == side, m.FeeRule.is_active.is_(True),
            m.FeeRule.effective_from <= now, or_(m.FeeRule.effective_to.is_(None), m.FeeRule.effective_to > now)).limit(1))
        if not rule: block(entity+'_fee_required', 'Настройте действующий тариф: '+('расчётный мерчант' if entity=='merchant' else 'агрегатор')+'.')
    status = access.status if access else 'not_activated'
    issue_reasons = []
    if mode == 'production' and not settings.PRODUCTION_ACTIVATION_ENABLED:
        issue_reasons.append('Владелец ещё не разрешил выпуск Production-ключей.')
    if (mode == 'production') != settings.is_production:
        issue_reasons.append('Режим сервера не совпадает с режимом БД.')
    if account.status != 'active' or account.is_archived:
        issue_reasons.append('Агрегатор должен быть активен.')
    current = next((k for k in keys if k.mode == mode and k.status != 'revoked'), None)
    return {'environment':mode, 'status':status,
        'label':'Sandbox' if mode=='sandbox' else 'Production · '+{'active':'активен','suspended':'приостановлен','not_activated':'не активирован'}[status],
        'blocking_reasons':reasons, 'can_activate':not reasons and status!='active',
        'can_suspend':mode=='production' and status=='active',
        'can_issue':not issue_reasons and current is None, 'can_rotate':not issue_reasons and current is not None,
        'issuance_blocking_reasons':issue_reasons,
        'keys':[{'id':str(k.id),'mode':k.mode,'status':k.status,'last_used_at':k.last_used_at} for k in keys]}


async def change_aggregator_access(db, identifier, actor, *, action, confirmation, reason, ip=None):
    require_root(actor)
    if action not in {'activate','suspend'}: raise IntegrationModeError('integration_action_invalid', 'Неизвестное действие.', 422)
    confirm(confirmation, 'PRODUCTION' if action=='activate' else 'SUSPEND', reason)
    account = await locked_account(db, identifier)
    mode = await verify_environment(db)
    state = await aggregator_integration_status(db, account)
    if mode != 'production' or (action=='activate' and state['blocking_reasons']):
        raise IntegrationModeError('production_activation_blocked', 'Production пока недоступен.', reasons=state['blocking_reasons'])
    if action=='activate' and account.callback_url:
        try: validate_public_webhook_url(account.callback_url)
        except ValueError as exc: raise IntegrationModeError('callback_not_public', 'Нужен публичный HTTPS-адрес callback.') from exc
    access = await db.get(m.AggregatorProductionAccess, account.id)
    target = 'active' if action=='activate' else 'suspended'
    if access and access.status == target: return state
    if not access and action=='suspend': raise IntegrationModeError('production_not_activated', 'Production ещё не активирован.')
    previous = access.status if access else 'not_activated'
    if not access:
        access = m.AggregatorProductionAccess(aggregator_id=account.id); db.add(access)
    access.status, access.changed_by, access.reason = target, actor.id, reason.strip()
    await audit(db, 'aggregator_production_'+action, 'aggregator', actor.id, account.id, ip,
                {'before':previous,'after':target,'environment':mode,'reason':reason.strip()})
    await db.flush()
    return await aggregator_integration_status(db, account)


async def change_aggregator_key(db, identifier, actor, *, action, mode, confirmation, reason, key_id=None, ip=None):
    require_root(actor)
    if action not in {'issue','rotate','suspend','revoke'}:
        raise IntegrationModeError('credential_action_invalid', 'Неизвестное действие с ключом.', 422)
    confirm(confirmation, mode.upper() if action in {'issue','rotate'} else action.upper(), reason)
    account = await locked_account(db, identifier)
    environment = await verify_environment(db)
    if mode not in {'sandbox','production'} or mode != environment:
        raise IntegrationModeError('api_key_environment_mismatch', 'Выпуск и управление ключом доступны только в соответствующем окружении.', 403)
    if action in {'issue','rotate'}:
        if mode=='production' and not settings.PRODUCTION_ACTIVATION_ENABLED:
            raise IntegrationModeError('owner_activation_disabled', 'Владелец ещё не разрешил выпуск Production-ключей.', 403)
        if account.status!='active' or account.is_archived:
            raise IntegrationModeError('aggregator_unavailable', 'Агрегатор должен быть активен.')
    current = await db.scalar(select(m.AggregatorApiKey).where(m.AggregatorApiKey.aggregator_id==account.id,
        m.AggregatorApiKey.mode==mode, m.AggregatorApiKey.status!='revoked'))
    if action=='issue' and current:
        raise IntegrationModeError('credential_already_issued', 'Ключ уже выпущен. Используйте ротацию.')
    if action!='issue':
        if not key_id: raise IntegrationModeError('credential_id_required', 'Выберите ключ.', 422)
        key = await db.get(m.AggregatorApiKey, key_id)
        if not key or key.aggregator_id!=account.id or key.mode!=mode:
            raise IntegrationModeError('credential_not_found', 'Ключ не найден.', 404)
        if key.status=='revoked':
            if action=='revoke': return key, None
            raise IntegrationModeError('credential_revoked', 'Ключ уже отозван.')
        if action=='suspend' and key.status=='suspended': return key, None
        previous = key.status
        key.status = 'suspended' if action=='suspend' else 'revoked'
        await db.flush()  # Release unique live-key slot before rotation.
    else: previous = 'not_issued'
    old_id = str(key.id) if action!='issue' else None
    secret = None
    if action in {'issue','rotate'}:
        secret = secrets.token_urlsafe(48)
        key = m.AggregatorApiKey(aggregator_id=account.id, mode=mode, status='active', created_by=actor.id,
            api_key=('ak_live_' if mode=='production' else 'ak_test_')+secrets.token_urlsafe(32), encrypted_secret=encrypt_secret(secret))
        db.add(key)
        # Compatibility projection used only for outbound callback signing.
        # Authentication never falls back to these account fields.
        account.api_key, account.secret_hash = key.api_key, key.encrypted_secret
        await db.flush()
    await audit(db, 'aggregator_key_'+action, 'aggregator', actor.id, account.id, ip,
        {'credential_id':str(key.id),'previous_credential_id':old_id,'mode':mode,'before':previous,'after':key.status,'reason':reason.strip()})
    await db.flush()
    return key, secret


async def authorize_aggregator_key(db, account, key):
    mode = await verify_environment(db)
    await db.refresh(key)  # Re-read revocation at the admission boundary.
    if key.mode != mode:
        raise IntegrationModeError('api_key_environment_mismatch', 'Ключ предназначен для другого окружения.', 403)
    if key.status != 'active':
        raise IntegrationModeError('aggregator_key_'+key.status, 'Ключ приостановлен или отозван.', 403)
    if mode=='production':
        access = await db.get(m.AggregatorProductionAccess, account.id, populate_existing=True)
        if not access or access.status!='active':
            raise IntegrationModeError('production_not_active', 'Доступ агрегатора в Production не активирован или приостановлен.', 403)
    # Admission reads the latest committed state; already admitted requests can
    # finish. No new financial lock order is introduced by authentication.
