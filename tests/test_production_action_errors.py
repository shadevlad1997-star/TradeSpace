"""Production command validation and diagnostic redirects, without database writes."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from fastapi import HTTPException, Request

from app.api.v1 import integration
from app.services import integration_modes as service
from app.web import routes
from app.web.ui_messages import resolve_ui_message


def actor():
    return SimpleNamespace(id=uuid4(), role='superadmin', twofa_enabled=True,
                           is_active=True, is_locked=False, is_archived=False)


def request():
    return Request({'type': 'http', 'method': 'POST', 'path': '/staff/cabinet',
                    'headers': [], 'client': ('127.0.0.1', 1000),
                    'state': {'request_id': 'production-request-test'}})


@pytest.mark.parametrize('confirmation,reason,code', [
    ('production', 'A valid activation reason', 'production_confirmation_required'),
    ('PRODUCTION', 'ok', 'production_reason_required'),
    ('PRODUCTION', ' ' * 20, 'production_reason_required'),
    ('PRODUCTION', 'x' * 1001, 'production_reason_required'),
])
def test_invalid_command_is_specific_and_does_not_touch_database(confirmation, reason, code):
    db = AsyncMock()
    with pytest.raises(service.IntegrationModeError) as error:
        asyncio.run(service.change_production_access(db, uuid4(), actor(), action='activate',
                    confirmation=confirmation, reason=reason))
    assert error.value.code == code and error.value.status == 422
    db.scalar.assert_not_awaited()
    db.flush.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.parametrize('code,reasons,expected', [
    ('production_confirmation_required', [], 'production_confirmation_required'),
    ('production_reason_required', [], 'production_reason_required'),
    ('production_activation_forbidden', [], 'production_activation_forbidden'),
    ('integration_environment_mismatch', [], 'integration_environment_mismatch'),
    ('production_activation_blocked', [], 'production_activation_blocked'),
    *[('production_activation_blocked', [{'code': code, 'message': 'server-only detail'}], code)
      for code in ('production_environment_required', 'owner_activation_disabled',
                   'merchant_unavailable', 'https_webhook_required', 'webhook_not_public',
                   'webhook_key_required', 'production_key_required', 'merchant_fee_required')],
])
def test_browser_rejection_preserves_code_context_and_logs_no_reason(monkeypatch, caplog, code, reasons, expected):
    user, merchant_id, db = actor(), uuid4(), AsyncMock()
    monkeypatch.setattr(routes, '_require_superadmin_web', AsyncMock(return_value=user))
    monkeypatch.setattr(service, 'change_production_access',
                        AsyncMock(side_effect=service.IntegrationModeError(code, 'internal error', reasons=reasons)))
    response = asyncio.run(routes.manage_merchant_production(request(), merchant_id, 'activate',
                            confirmation='PRODUCTION', reason='do-not-log-this-reason', db=db))
    assert response.status_code == 303
    location = urlsplit(response.headers['location'])
    assert location.path == '/staff/cabinet/tradespace/integrations'
    query = {key: values[0] for key, values in parse_qs(location.query).items()}
    assert query == {'tab': 'credentials', 'id': str(merchant_id), 'ui_error': expected}
    assert resolve_ui_message(query).code == expected
    db.commit.assert_not_awaited()
    record = next(r for r in caplog.records if r.message == 'merchant_production_action_rejected')
    assert record.merchant_id == str(merchant_id) and record.actor_id == str(user.id)
    assert record.request_id == 'production-request-test' and record.error_code == code
    assert record.blocking_codes == [item['code'] for item in reasons]
    assert 'do-not-log-this-reason' not in str(record.__dict__)
    assert 'server-only detail' not in str(record.__dict__)


def test_unknown_error_detail_cannot_be_injected_into_ui(monkeypatch):
    monkeypatch.setattr(routes, '_require_superadmin_web', AsyncMock(return_value=actor()))
    monkeypatch.setattr(service, 'change_production_access', AsyncMock(side_effect=
        service.IntegrationModeError('<script>secret</script>', '<script>secret</script>')))
    response = asyncio.run(routes.manage_merchant_production(request(), uuid4(), 'activate',
                            confirmation='PRODUCTION', reason='A valid reason', db=AsyncMock()))
    assert 'ui_error=action_failed' in response.headers['location']
    assert 'secret' not in response.headers['location']


def test_api_rejection_preserves_structured_detail_and_request_id(monkeypatch, caplog):
    user, merchant_id, db = actor(), uuid4(), AsyncMock()
    error = service.IntegrationModeError('production_reason_required', 'Reason too short', 422)
    monkeypatch.setattr(integration, 'change_production_access', AsyncMock(side_effect=error))
    with pytest.raises(HTTPException) as result:
        asyncio.run(integration.production(merchant_id, 'activate',
                    integration.ProductionCommand(confirmation='PRODUCTION', reason=' ' * 10),
                    request(), actor=user, db=db))
    assert result.value.status_code == 422
    assert result.value.detail['code'] == error.code
    db.commit.assert_not_awaited()
    record = next(r for r in caplog.records if r.message == 'merchant_production_action_rejected')
    assert record.request_id == 'production-request-test'
