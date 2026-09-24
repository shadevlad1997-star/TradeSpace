import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace

from app.core.config import settings
from app.core.enums import Role
from app.core.security import encrypt_secret, verify_password
from app.models import AuditLog, User
from scripts import bootstrap_superadmin, migrate, seed


ROOT = Path(__file__).resolve().parents[1]


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _Scalars:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


class _RowsResult:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return _Scalars(self.values)


class _FakeBootstrapDb:
    def __init__(self, results):
        self.results = list(results)
        self.added = []

    async def execute(self, _query):
        return _ScalarResult(self.results.pop(0))

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        for value in self.added:
            if isinstance(value, User) and value.id is None:
                value.id = uuid.uuid4()


class _FakeSeedDb:
    def __init__(self, requisites):
        self.requisites = requisites

    async def execute(self, _query):
        return _RowsResult(self.requisites)


def test_production_processes_do_not_run_migrations_or_seed():
    compose = (ROOT / 'docker-compose.production.yml').read_text(
        encoding='utf-8'
    )
    api = compose.split('\n  api:', 1)[1].split('\n  migrate:', 1)[0]
    migrate_service = compose.split('\n  migrate:', 1)[1].split(
        '\n  worker:', 1
    )[0]
    worker = compose.split('\n  worker:', 1)[1].split('\n  beat:', 1)[0]
    beat = compose.split('\n  beat:', 1)[1].split('\n  nginx:', 1)[0]

    assert 'command: uvicorn app.main:app' in api
    for service in (api, worker, beat):
        assert 'alembic' not in service
        assert 'scripts.seed' not in service
        assert 'bootstrap_superadmin' not in service

    assert 'profiles:' in migrate_service
    assert '- migrate' in migrate_service
    assert 'command: python -m scripts.migrate' in migrate_service
    assert 'scripts.seed' not in migrate_service


def test_release_script_migrates_before_starting_application_services():
    script = (ROOT / 'scripts' / 'deploy_production.ps1').read_text(
        encoding='utf-8'
    )
    stop_position = script.index('stop api worker beat')
    migrate_position = script.index(
        '--profile migrate run --rm migrate',
        stop_position,
    )
    start_position = script.index(
        'up -d --build api worker beat',
        migrate_position,
    )

    assert stop_position < migrate_position < start_position
    assert 'BackupVerified' in script
    assert 'Migration job failed. Application services were not started.' in script


def test_migrate_preflight_does_not_upgrade(monkeypatch, capsys):
    monkeypatch.setattr(
        migrate,
        '_revision_state',
        lambda _config: (('0019_ai_office_config',), ('0020_head',)),
    )

    def forbidden_upgrade(*_args, **_kwargs):
        raise AssertionError('upgrade must not run during preflight')

    monkeypatch.setattr(migrate.command, 'upgrade', forbidden_upgrade)

    assert migrate.run(preflight=True) == 0
    output = capsys.readouterr().out
    assert 'current=0019_ai_office_config' in output
    assert 'head=0020_head' in output
    assert settings.SYNC_DATABASE_URL not in output


def test_bootstrap_creates_first_superadmin(monkeypatch):
    monkeypatch.setattr(
        settings,
        'SUPERADMIN_EMAIL',
        'first-superadmin@example.test',
    )
    monkeypatch.setattr(
        settings,
        'SUPERADMIN_PASSWORD',
        'first-superadmin-password',
    )
    monkeypatch.setattr(
        settings,
        'SUPERADMIN_2FA_SECRET',
        'JBSWY3DPEHPK3PXP',
    )
    db = _FakeBootstrapDb([None, None])

    result = asyncio.run(
        bootstrap_superadmin.bootstrap_superadmin(db)
    )

    assert result == 'created'
    user = next(value for value in db.added if isinstance(value, User))
    audit = next(value for value in db.added if isinstance(value, AuditLog))
    assert user.role == Role.superadmin.value
    assert user.twofa_enabled is True
    assert verify_password('first-superadmin-password', user.password_hash)
    assert audit.action == 'superadmin_bootstrap_created'


def test_repeated_bootstrap_preserves_existing_account(monkeypatch):
    monkeypatch.setattr(
        settings,
        'SUPERADMIN_EMAIL',
        'existing-superadmin@example.test',
    )
    existing = User(
        id=uuid.uuid4(),
        email=settings.SUPERADMIN_EMAIL,
        password_hash='original-password-hash',
        role=Role.superadmin.value,
        is_active=False,
        is_locked=True,
        failed_login_count=9,
        twofa_secret=encrypt_secret('ORIGINAL2FASEED1'),
        twofa_enabled=True,
    )
    original = {
        'password_hash': existing.password_hash,
        'role': existing.role,
        'is_active': existing.is_active,
        'is_locked': existing.is_locked,
        'failed_login_count': existing.failed_login_count,
        'twofa_secret': existing.twofa_secret,
        'twofa_enabled': existing.twofa_enabled,
    }
    db = _FakeBootstrapDb([existing])

    result = asyncio.run(
        bootstrap_superadmin.bootstrap_superadmin(
            db,
            update_existing=False,
        )
    )

    assert result == 'unchanged'
    assert db.added == []
    assert {
        'password_hash': existing.password_hash,
        'role': existing.role,
        'is_active': existing.is_active,
        'is_locked': existing.is_locked,
        'failed_login_count': existing.failed_login_count,
        'twofa_secret': existing.twofa_secret,
        'twofa_enabled': existing.twofa_enabled,
    } == original


def test_explicit_bootstrap_update_preserves_lock_state(monkeypatch):
    monkeypatch.setattr(
        settings,
        'SUPERADMIN_EMAIL',
        'promote-existing@example.test',
    )
    monkeypatch.setattr(
        settings,
        'SUPERADMIN_PASSWORD',
        'replacement-superadmin-password',
    )
    monkeypatch.setattr(
        settings,
        'SUPERADMIN_2FA_SECRET',
        'JBSWY3DPEHPK3PXP',
    )
    existing = User(
        id=uuid.uuid4(),
        email=settings.SUPERADMIN_EMAIL,
        password_hash='original-password-hash',
        role=Role.admin.value,
        is_active=False,
        is_locked=True,
        failed_login_count=11,
        twofa_secret=encrypt_secret('ORIGINAL2FASEED1'),
        twofa_enabled=False,
    )
    db = _FakeBootstrapDb([existing])

    result = asyncio.run(
        bootstrap_superadmin.bootstrap_superadmin(
            db,
            update_existing=True,
        )
    )

    assert result == 'updated'
    assert existing.role == Role.superadmin.value
    assert existing.twofa_enabled is True
    assert existing.is_active is False
    assert existing.is_locked is True
    assert existing.failed_login_count == 11
    assert verify_password(
        'replacement-superadmin-password',
        existing.password_hash,
    )
    audit = next(value for value in db.added if isinstance(value, AuditLog))
    assert audit.action == 'superadmin_bootstrap_updated'
    assert audit.details['lock_state_preserved'] is True


def test_seed_is_rejected_when_app_env_is_production(monkeypatch):
    monkeypatch.setenv('APP_ENV', 'production')

    try:
        seed.assert_seed_environment()
    except RuntimeError as exc:
        assert 'restricted' in str(exc)
    else:
        raise AssertionError('production seed must be rejected')


def test_seed_decryption_failure_does_not_disable_requisite():
    requisite = SimpleNamespace(
        value_encrypted='enc:v1:not-a-valid-fernet-token',
        enabled=True,
        status='active',
    )
    db = _FakeSeedDb([requisite])

    try:
        asyncio.run(seed.normalize_requisites_plaintext(db))
    except RuntimeError as exc:
        assert 'no seed changes were committed' in str(exc)
    else:
        raise AssertionError('unreadable encrypted value must fail closed')

    assert requisite.enabled is True
    assert requisite.status == 'active'
