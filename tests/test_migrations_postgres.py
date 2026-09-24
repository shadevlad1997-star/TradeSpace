import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql

from app.core.security import decrypt_secret, encrypt_secret


ROOT = Path(__file__).resolve().parents[1]


def _sync_url(value: str) -> str:
    return (
        value.replace('postgresql+asyncpg://', 'postgresql://', 1)
        .replace('postgresql+psycopg://', 'postgresql://', 1)
    )


def _database_urls():
    source = os.getenv('TEST_DATABASE_URL', '')
    if not source:
        pytest.skip('TEST_DATABASE_URL is required for migration tests')
    sync_source = _sync_url(source)
    parts = urlsplit(sync_source)
    database_name = f'tradespace_migration_{uuid.uuid4().hex[:16]}'
    admin_url = urlunsplit(
        (parts.scheme, parts.netloc, '/postgres', parts.query, parts.fragment)
    )
    database_url = urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            f'/{database_name}',
            parts.query,
            parts.fragment,
        )
    )
    return admin_url, database_url, database_name


def _alembic(database_url: str, *args: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        'DATABASE_URL': database_url.replace(
            'postgresql://',
            'postgresql+asyncpg://',
            1,
        ),
        'SYNC_DATABASE_URL': database_url.replace(
            'postgresql://',
            'postgresql+psycopg://',
            1,
        ),
    }
    return subprocess.run(
        [sys.executable, '-m', 'alembic', *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _insert_legacy_allocation(
    db,
    *,
    merchant_id,
    account_id,
    created_at,
    allocation_status,
    deposit_status,
    rolling_applied_usdt=Decimal('0'),
    rolling_applied_rub=Decimal('0'),
    settle_credited_rub=Decimal('0'),
):
    deposit_id = uuid.uuid4()
    allocation_id = uuid.uuid4()
    merchant_payable_rub = Decimal('4000')
    merchant_payable_usdt = Decimal('40')
    db.execute(
        """
        INSERT INTO deposits (
            id, merchant_id, external_id, amount, currency, method,
            status, metadata_json, expires_at, created_at, updated_at
        )
        VALUES (
            %s, %s, %s, 5000, 'RUB', 'sbp', %s, '{}'::json,
            %s, %s, %s
        )
        """,
        (
            deposit_id,
            merchant_id,
            f'legacy-fixture-{uuid.uuid4().hex}',
            deposit_status,
            created_at + timedelta(minutes=15),
            created_at,
            created_at,
        ),
    )
    db.execute(
        """
        INSERT INTO merchant_rolling_allocations (
            id, deposit_id, merchant_id, rolling_account_id,
            gross_rub, merchant_fee_percent_snapshot,
            merchant_fee_rub, merchant_net_rub,
            rapira_rate_rub, rapira_rate_symbol, rapira_rate_side,
            rapira_rate_source, rapira_rate_field,
            rapira_rate_updated_at, rapira_provider_timestamp,
            rapira_fetched_at, rapira_freshness_basis,
            merchant_net_usdt, rolling_applied_usdt,
            rolling_applied_rub, settle_credited_rub,
            status, finalized_at, created_at, updated_at
        )
        VALUES (
            %s, %s, %s, %s,
            5000, 20, 1000, %s,
            100, 'USDT/RUB', 'ask', 'rapira_live', 'askPrice',
            %s, NULL, %s, 'fetched_at',
            %s, %s, %s, %s,
            %s, %s, %s, %s
        )
        """,
        (
            allocation_id,
            deposit_id,
            merchant_id,
            account_id,
            merchant_payable_rub,
            created_at,
            created_at,
            merchant_payable_usdt,
            rolling_applied_usdt,
            rolling_applied_rub,
            settle_credited_rub,
            allocation_status,
            (
                None
                if allocation_status == 'pending'
                else created_at + timedelta(minutes=5)
            ),
            created_at,
            created_at,
        ),
    )
    return allocation_id, deposit_id


def test_postgres_clean_downgrade_and_funded_rolling_downgrade_guard():
    admin_url, database_url, database_name = _database_urls()
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(
            sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database_name))
        )
    try:
        upgrade = _alembic(database_url, 'upgrade', 'head')
        assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr

        clean_downgrade = _alembic(
            database_url,
            'downgrade',
            '0014_deposit_finance',
        )
        assert clean_downgrade.returncode == 0, (
            clean_downgrade.stdout + clean_downgrade.stderr
        )
        reupgrade = _alembic(database_url, 'upgrade', 'head')
        assert reupgrade.returncode == 0, reupgrade.stdout + reupgrade.stderr

        owner_id = uuid.uuid4()
        merchant_id = uuid.uuid4()
        account_id = uuid.uuid4()
        ledger_id = uuid.uuid4()
        with psycopg.connect(database_url) as db:
            db.execute(
                """
                INSERT INTO users (
                    id,
                    email,
                    password_hash,
                    role,
                    is_active,
                    is_locked,
                    failed_login_count,
                    twofa_enabled
                )
                VALUES (%s, %s, %s, 'merchant', true, false, 0, false)
                """,
                (
                    owner_id,
                    f'migration-{uuid.uuid4().hex}@example.test',
                    'not-used-in-test',
                ),
            )
            db.execute(
                """
                INSERT INTO merchants (
                    id,
                    owner_id,
                    name,
                    ip_whitelist,
                    sandbox_mode
                )
                VALUES (
                    %s,
                    %s,
                    'Migration guard merchant',
                    '[]'::json,
                    true
                )
                """,
                (merchant_id, owner_id),
            )
            db.execute(
                """
                INSERT INTO merchant_rolling_accounts (
                    id,
                    merchant_id,
                    principal_usdt,
                    recovered_usdt,
                    outstanding_usdt,
                    status
                )
                VALUES (%s, %s, 1, 0, 1, 'active')
                """,
                (account_id, merchant_id),
            )
            db.execute(
                """
                INSERT INTO merchant_rolling_ledger_entries (
                    id,
                    rolling_account_id,
                    merchant_id,
                    entry_type,
                    amount_usdt,
                    network,
                    destination_address,
                    tx_hash,
                    funded_at,
                    reason,
                    idempotency_key
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    'funding',
                    1,
                    'TESTNET',
                    'test-only-address',
                    %s,
                    now(),
                    'migration guard test',
                    %s
                )
                """,
                (
                    ledger_id,
                    account_id,
                    merchant_id,
                    f'test-{uuid.uuid4().hex}',
                    f'test-{uuid.uuid4().hex}',
                ),
            )
            db.commit()
            with pytest.raises(
                psycopg.errors.RaiseException,
                match='rolling ledger entries are immutable',
            ):
                db.execute(
                    """
                    UPDATE merchant_rolling_ledger_entries
                       SET reason = 'forbidden update'
                     WHERE id = %s
                    """,
                    (ledger_id,),
                )
            db.rollback()
            with pytest.raises(
                psycopg.errors.RaiseException,
                match='rolling ledger entries are immutable',
            ):
                db.execute(
                    """
                    DELETE FROM merchant_rolling_ledger_entries
                     WHERE id = %s
                    """,
                    (ledger_id,),
                )
            db.rollback()

        blocked = _alembic(
            database_url,
            'downgrade',
            '0014_deposit_finance',
        )
        assert blocked.returncode != 0
        assert (
            'refusing destructive Rolling downgrade after funding or traffic'
            in blocked.stdout + blocked.stderr
        )
        with psycopg.connect(database_url) as verify:
            assert verify.execute(
                'SELECT version_num FROM alembic_version'
            ).fetchone()[0] == '0024_required_schema_indexes'
            assert verify.execute(
                'SELECT count(*) FROM merchant_rolling_ledger_entries'
            ).fetchone()[0] == 1
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(
                    sql.Identifier(database_name)
                )
            )


def test_postgres_teamlead_clean_downgrade_immutable_trigger_and_finance_guard():
    admin_url, database_url, database_name = _database_urls()
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(
            sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database_name))
        )
    try:
        upgrade = _alembic(database_url, 'upgrade', 'head')
        assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr
        clean = _alembic(
            database_url,
            'downgrade',
            '0016_rolling_rapira_freshness',
        )
        assert clean.returncode == 0, clean.stdout + clean.stderr
        reupgrade = _alembic(database_url, 'upgrade', 'head')
        assert reupgrade.returncode == 0, reupgrade.stdout + reupgrade.stderr

        teamlead_id = uuid.uuid4()
        balance_id = uuid.uuid4()
        ledger_id = uuid.uuid4()
        with psycopg.connect(database_url) as db:
            db.execute(
                """
                INSERT INTO users (
                    id,
                    email,
                    password_hash,
                    role,
                    is_active,
                    is_locked,
                    failed_login_count,
                    twofa_enabled
                )
                VALUES (%s, %s, %s, 'teamlead', true, false, 0, true)
                """,
                (
                    teamlead_id,
                    f'teamlead-migration-{uuid.uuid4().hex}@example.test',
                    'not-used-in-test',
                ),
            )
            db.execute(
                """
                INSERT INTO teamlead_balances (
                    id,
                    teamlead_id,
                    available_rub,
                    frozen_rub,
                    debt_rub,
                    total_earned_rub,
                    total_paid_rub
                )
                VALUES (%s, %s, 1, 0, 0, 1, 0)
                """,
                (balance_id, teamlead_id),
            )
            db.execute(
                """
                INSERT INTO teamlead_ledger_entries (
                    id,
                    teamlead_id,
                    entry_type,
                    amount_rub,
                    available_after,
                    frozen_after,
                    debt_after,
                    idempotency_key,
                    reason
                )
                VALUES (%s, %s, 'manual_adjustment', 1, 1, 0, 0, %s, %s)
                """,
                (
                    ledger_id,
                    teamlead_id,
                    f'teamlead-migration-{uuid.uuid4().hex}',
                    'migration guard test',
                ),
            )
            db.commit()
            with pytest.raises(
                psycopg.errors.RaiseException,
                match='teamlead ledger entries are immutable',
            ):
                db.execute(
                    """
                    UPDATE teamlead_ledger_entries
                       SET reason = 'forbidden'
                     WHERE id = %s
                    """,
                    (ledger_id,),
                )
            db.rollback()
            with pytest.raises(
                psycopg.errors.RaiseException,
                match='teamlead ledger entries are immutable',
            ):
                db.execute(
                    'DELETE FROM teamlead_ledger_entries WHERE id = %s',
                    (ledger_id,),
                )
            db.rollback()

        blocked = _alembic(
            database_url,
            'downgrade',
            '0016_rolling_rapira_freshness',
        )
        assert blocked.returncode != 0
        assert (
            'refusing destructive TeamLead downgrade after financial activity'
            in blocked.stdout + blocked.stderr
        )
        with psycopg.connect(database_url) as verify:
            assert verify.execute(
                'SELECT version_num FROM alembic_version'
            ).fetchone()[0] == '0024_required_schema_indexes'
            assert verify.execute(
                'SELECT count(*) FROM teamlead_ledger_entries'
            ).fetchone()[0] == 1
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(
                    sql.Identifier(database_name)
                )
            )


def test_postgres_teamlead_role_is_stored_as_string_and_0017_migration_has_expected_indexes():
    admin_url, database_url, database_name = _database_urls()
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(
            sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database_name))
        )
    try:
        upgrade = _alembic(database_url, 'upgrade', '0016_rolling_rapira_freshness')
        assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr
        to_0017 = _alembic(database_url, 'upgrade', '0017_teamlead')
        assert to_0017.returncode == 0, to_0017.stdout + to_0017.stderr
        teamlead_id = uuid.uuid4()
        trader_id = uuid.uuid4()
        with psycopg.connect(database_url) as db:
            role_column = db.execute(
                """
                SELECT data_type
                  FROM information_schema.columns
                 WHERE table_name = 'users'
                   AND column_name = 'role'
                """
            ).fetchone()
            assert role_column is not None
            assert role_column[0] == 'character varying'
            db.execute(
                """
                INSERT INTO users (
                    id,
                    email,
                    password_hash,
                    role,
                    is_active,
                    is_locked,
                    failed_login_count,
                    twofa_enabled
                )
                VALUES
                    (%s, %s, 'unused', 'teamlead', true, false, 0, true),
                    (%s, %s, 'unused', 'trader', true, false, 0, false)
                """,
                (
                    teamlead_id,
                    f'teamlead-clean-{uuid.uuid4().hex}@example.test',
                    trader_id,
                    f'trader-clean-{uuid.uuid4().hex}@example.test',
                ),
            )
            db.execute(
                """
                INSERT INTO teamlead_trader_assignments (
                    id,
                    teamlead_id,
                    trader_id,
                    commission_percent,
                    effective_from,
                    creation_reason
                )
                VALUES (%s, %s, %s, 0.5, now(), 'clean downgrade fixture')
                """,
                (uuid.uuid4(), teamlead_id, trader_id),
            )
            index_names = {
                row[0]
                for row in db.execute(
                    """
                    SELECT indexname
                      FROM pg_indexes
                     WHERE schemaname = 'public'
                       AND indexname IN (
                           'uq_teamlead_assignment_active_trader',
                           'uq_teamlead_settlement_pending',
                           'uq_teamlead_settlement_completed_tx'
                       )
                    """
                ).fetchall()
            }
            assert index_names == {
                'uq_teamlead_assignment_active_trader',
                'uq_teamlead_settlement_pending',
                'uq_teamlead_settlement_completed_tx',
            }
            constraint_names = {
                row[0]
                for row in db.execute(
                    """
                    SELECT conname
                      FROM pg_constraint
                     WHERE conrelid = 'teamlead_settlements'::regclass
                       AND conname IN (
                           'ck_teamlead_settlement_total_rub',
                           'ck_teamlead_settlement_state'
                       )
                    """
                ).fetchall()
            }
            assert constraint_names == {
                'ck_teamlead_settlement_total_rub',
                'ck_teamlead_settlement_state',
            }
            assert db.execute(
                """
                SELECT 1
                  FROM pg_indexes
                 WHERE schemaname = 'public'
                   AND tablename = 'teamlead_settlements'
                   AND indexname = 'uq_teamlead_settlement_completed_tx'
                """
            ).fetchone() is not None
            db.commit()
        rollback = _alembic(database_url, 'downgrade', '0016_rolling_rapira_freshness')
        assert rollback.returncode == 0, rollback.stdout + rollback.stderr
        with psycopg.connect(database_url) as db:
            assert db.execute(
                'SELECT role FROM users WHERE id = %s',
                (teamlead_id,),
            ).fetchone()[0] == 'teamlead'
            assert db.execute(
                "SELECT to_regclass('public.teamlead_trader_assignments')"
            ).fetchone()[0] is None
        reupgrade = _alembic(database_url, 'upgrade', '0017_teamlead')
        assert reupgrade.returncode == 0, reupgrade.stdout + reupgrade.stderr
        with psycopg.connect(database_url) as db:
            assert db.execute(
                'SELECT role FROM users WHERE id = %s',
                (teamlead_id,),
            ).fetchone()[0] == 'teamlead'
            assert db.execute(
                'SELECT count(*) FROM teamlead_trader_assignments'
            ).fetchone()[0] == 0
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(
                    sql.Identifier(database_name)
                )
            )


def test_postgres_v2_clean_0017_to_0018_to_0019_downgrade_and_reupgrade():
    admin_url, database_url, database_name = _database_urls()
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(
            sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database_name))
        )
    try:
        to_0017 = _alembic(database_url, 'upgrade', '0017_teamlead')
        assert to_0017.returncode == 0, to_0017.stdout + to_0017.stderr
        to_0018 = _alembic(
            database_url,
            'upgrade',
            '0018_platform_crypto_wallet',
        )
        assert to_0018.returncode == 0, to_0018.stdout + to_0018.stderr
        with psycopg.connect(database_url) as db:
            assert db.execute(
                'SELECT version_num FROM alembic_version'
            ).fetchone()[0] == '0018_platform_crypto_wallet'
            assert db.execute(
                """
                SELECT indexdef
                  FROM pg_indexes
                 WHERE schemaname = 'public'
                   AND indexname = 'uq_platform_crypto_wallet_active'
                """
            ).fetchone()[0].endswith('WHERE is_active')
            assert db.execute(
                """
                SELECT 1
                  FROM pg_trigger
                 WHERE tgname = 'trg_platform_crypto_wallet_history'
                   AND NOT tgisinternal
                """
            ).fetchone() is not None

        to_0019 = _alembic(
            database_url,
            'upgrade',
            '0019_ai_office_config',
        )
        assert to_0019.returncode == 0, to_0019.stdout + to_0019.stderr
        with psycopg.connect(database_url) as db:
            assert db.execute(
                'SELECT version_num FROM alembic_version'
            ).fetchone()[0] == '0019_ai_office_config'
            columns = {
                row[0]
                for row in db.execute(
                    """
                    SELECT column_name
                      FROM information_schema.columns
                     WHERE table_schema = 'public'
                       AND table_name = 'ai_integration_configs'
                    """
                ).fetchall()
            }
            assert {
                'encrypted_api_key',
                'encrypted_bearer_token',
                'encrypted_hmac_secret',
                'inbound_commands_enabled',
                'last_error_message_redacted',
            }.issubset(columns)

        clean = _alembic(database_url, 'downgrade', '0017_teamlead')
        assert clean.returncode == 0, clean.stdout + clean.stderr
        with psycopg.connect(database_url) as db:
            assert db.execute(
                "SELECT to_regclass('public.platform_crypto_wallets')"
            ).fetchone()[0] is None
            assert db.execute(
                "SELECT to_regclass('public.ai_integration_configs')"
            ).fetchone()[0] is None
        reupgrade = _alembic(database_url, 'upgrade', 'head')
        assert reupgrade.returncode == 0, reupgrade.stdout + reupgrade.stderr
        with psycopg.connect(database_url) as db:
            assert db.execute(
                'SELECT version_num FROM alembic_version'
            ).fetchone()[0] == '0027_aggregator_credentials'
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(
                    sql.Identifier(database_name)
                )
            )


def test_postgres_v2_wallet_history_downgrade_guard():
    admin_url, database_url, database_name = _database_urls()
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(
            sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database_name))
        )
    try:
        upgrade = _alembic(database_url, 'upgrade', 'head')
        assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr
        actor_id = uuid.uuid4()
        wallet_id = uuid.uuid4()
        with psycopg.connect(database_url) as db:
            db.execute(
                """
                INSERT INTO users (
                    id,
                    email,
                    password_hash,
                    role,
                    is_active,
                    is_locked,
                    failed_login_count,
                    twofa_enabled
                )
                VALUES (%s, %s, 'unused', 'superadmin', true, false, 0, true)
                """,
                (
                    actor_id,
                    f'wallet-guard-{uuid.uuid4().hex}@example.test',
                ),
            )
            db.execute(
                """
                INSERT INTO platform_crypto_wallets (
                    id,
                    asset,
                    network,
                    address,
                    is_active,
                    version,
                    created_by,
                    change_reason
                )
                VALUES (%s, 'USDT', 'TRC20', %s, true, 1, %s, %s)
                """,
                (
                    wallet_id,
                    'TTestOnlyWalletAddressForMigration1',
                    actor_id,
                    'migration guard test',
                ),
            )
            db.commit()
            with pytest.raises(
                psycopg.errors.RaiseException,
                match='platform wallet history is immutable',
            ):
                db.execute(
                    """
                    UPDATE platform_crypto_wallets
                       SET address = 'TForbiddenOverwriteForMigration'
                     WHERE id = %s
                    """,
                    (wallet_id,),
                )
            db.rollback()
            with pytest.raises(
                psycopg.errors.RaiseException,
                match='platform wallet history is immutable',
            ):
                db.execute(
                    'DELETE FROM platform_crypto_wallets WHERE id = %s',
                    (wallet_id,),
                )
            db.rollback()

        blocked = _alembic(database_url, 'downgrade', '0017_teamlead')
        assert blocked.returncode != 0
        assert (
            'refusing destructive platform wallet downgrade after wallet history exists'
            in blocked.stdout + blocked.stderr
        )
        with psycopg.connect(database_url) as db:
            assert db.execute(
                'SELECT version_num FROM alembic_version'
            ).fetchone()[0] == '0024_required_schema_indexes'
            assert db.execute(
                'SELECT count(*) FROM platform_crypto_wallets'
            ).fetchone()[0] == 1
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(
                    sql.Identifier(database_name)
                )
            )


def test_postgres_v2_ai_config_secret_downgrade_guard():
    admin_url, database_url, database_name = _database_urls()
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(
            sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database_name))
        )
    try:
        upgrade = _alembic(database_url, 'upgrade', 'head')
        assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr
        encrypted = encrypt_secret(f'migration-only-{uuid.uuid4().hex}')
        with psycopg.connect(database_url) as db:
            db.execute(
                """
                INSERT INTO ai_integration_configs (
                    id,
                    provider,
                    enabled,
                    environment,
                    base_url,
                    health_path,
                    auth_type,
                    encrypted_bearer_token,
                    timeout_seconds,
                    connect_timeout_seconds,
                    max_retries,
                    verify_tls,
                    selected_events,
                    inbound_commands_enabled
                )
                VALUES (
                    %s,
                    'veyra_ai_office',
                    false,
                    'local',
                    'http://127.0.0.1:8000',
                    '/api/health',
                    'bearer',
                    %s,
                    5,
                    2,
                    0,
                    false,
                    '[]'::json,
                    false
                )
                """,
                (uuid.uuid4(), encrypted),
            )
            db.commit()

        blocked = _alembic(
            database_url,
            'downgrade',
            '0018_platform_crypto_wallet',
        )
        assert blocked.returncode != 0
        assert (
            'refusing destructive AI Office downgrade after configuration exists'
            in blocked.stdout + blocked.stderr
        )
        with psycopg.connect(database_url) as db:
            assert db.execute(
                'SELECT version_num FROM alembic_version'
            ).fetchone()[0] == '0024_required_schema_indexes'
            stored = db.execute(
                'SELECT encrypted_bearer_token FROM ai_integration_configs'
            ).fetchone()[0]
            assert stored == encrypted
            assert decrypt_secret(stored).startswith('migration-only-')
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(
                    sql.Identifier(database_name)
                )
            )


def test_postgres_0019_to_0020_preserves_legacy_rolling_and_guards_new_transfers():
    admin_url, database_url, database_name = _database_urls()
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(
            sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database_name))
        )
    try:
        to_0019 = _alembic(
            database_url,
            'upgrade',
            '0019_ai_office_config',
        )
        assert to_0019.returncode == 0, to_0019.stdout + to_0019.stderr

        owner_id = uuid.uuid4()
        plain_owner_id = uuid.uuid4()
        merchant_id = uuid.uuid4()
        plain_merchant_id = uuid.uuid4()
        account_id = uuid.uuid4()
        deposit_id = uuid.uuid4()
        allocation_id = uuid.uuid4()
        ledger_ids = (uuid.uuid4(), uuid.uuid4())
        legacy_mode_column = 'settlement_' + 'mode'
        legacy_default_mode = 'post' + 'paid'
        with psycopg.connect(database_url) as db:
            db.execute(
                """
                INSERT INTO users (
                    id, email, password_hash, role, is_active, is_locked,
                    failed_login_count, twofa_enabled
                )
                VALUES
                    (%s, %s, 'unused', 'merchant', true, false, 0, false),
                    (%s, %s, 'unused', 'merchant', true, false, 0, false)
                """,
                (
                    owner_id,
                    f'legacy-rolling-{uuid.uuid4().hex}@example.test',
                    plain_owner_id,
                    f'plain-merchant-{uuid.uuid4().hex}@example.test',
                ),
            )
            db.execute(
                """
                INSERT INTO merchants (
                    id, owner_id, name, ip_whitelist, sandbox_mode
                )
                VALUES
                    (%s, %s, 'Legacy Rolling merchant', '[]'::json, true),
                    (%s, %s, 'Plain merchant', '[]'::json, true)
                """,
                (
                    merchant_id,
                    owner_id,
                    plain_merchant_id,
                    plain_owner_id,
                ),
            )
            db.execute(
                f"""
                INSERT INTO merchant_finance_profiles (
                    id, merchant_id, {legacy_mode_column}, is_active
                )
                VALUES
                    (%s, %s, 'rolling', true),
                    (%s, %s, %s, true)
                """,
                (
                    uuid.uuid4(),
                    merchant_id,
                    uuid.uuid4(),
                    plain_merchant_id,
                    legacy_default_mode,
                ),
            )
            db.execute(
                """
                INSERT INTO balances (
                    id, merchant_id, currency, available, frozen
                )
                VALUES
                    (%s, %s, 'RUB', 123.45, 6.78),
                    (%s, %s, 'RUB', 99.01, 0)
                """,
                (
                    uuid.uuid4(),
                    merchant_id,
                    uuid.uuid4(),
                    plain_merchant_id,
                ),
            )
            db.execute(
                """
                INSERT INTO deposits (
                    id, merchant_id, external_id, amount, currency, method,
                    status, metadata_json, expires_at, created_at, updated_at
                )
                VALUES (
                    %s, %s, %s, 5000, 'RUB', 'sbp', 'paid', '{}'::json,
                    now() + interval '15 minutes',
                    now() - interval '1 day',
                    now() - interval '1 day'
                )
                """,
                (
                    deposit_id,
                    merchant_id,
                    f'legacy-{uuid.uuid4().hex}',
                ),
            )
            db.execute(
                """
                INSERT INTO merchant_rolling_accounts (
                    id, merchant_id, principal_usdt, recovered_usdt,
                    outstanding_usdt, status, created_at, updated_at
                )
                VALUES (
                    %s, %s, 100, 40, 60, 'active',
                    now() - interval '2 days',
                    now() - interval '1 hour'
                )
                """,
                (account_id, merchant_id),
            )
            db.execute(
                """
                INSERT INTO merchant_rolling_allocations (
                    id, deposit_id, merchant_id, rolling_account_id,
                    gross_rub, merchant_fee_percent_snapshot,
                    merchant_fee_rub, merchant_net_rub,
                    rapira_rate_rub, rapira_rate_symbol, rapira_rate_side,
                    rapira_rate_source, rapira_rate_field,
                    rapira_rate_updated_at, rapira_provider_timestamp,
                    rapira_fetched_at, rapira_freshness_basis,
                    merchant_net_usdt, rolling_applied_usdt,
                    rolling_applied_rub, settle_credited_rub,
                    status, finalized_at, created_at, updated_at
                )
                VALUES (
                    %s, %s, %s, %s,
                    5000, 20, 1000, 4000,
                    100, 'USDT/RUB', 'ask',
                    'rapira_live', 'askPrice',
                    now() - interval '1 day', NULL,
                    now() - interval '1 day', 'fetched_at',
                    40, 40, 4000, 0,
                    'paid', now() - interval '23 hours',
                    now() - interval '1 day',
                    now() - interval '23 hours'
                )
                """,
                (
                    allocation_id,
                    deposit_id,
                    merchant_id,
                    account_id,
                ),
            )
            db.execute(
                """
                INSERT INTO merchant_rolling_ledger_entries (
                    id, rolling_account_id, merchant_id, deposit_id,
                    entry_type, amount_usdt, amount_rub, rate_rub,
                    reason, idempotency_key, metadata_json,
                    created_at, updated_at
                )
                VALUES
                    (
                        %s, %s, %s, NULL, 'funding', 100, NULL, NULL,
                        'legacy funding evidence', %s, '{}'::json,
                        now() - interval '2 days', now() - interval '2 days'
                    ),
                    (
                        %s, %s, %s, %s, 'recovery', 40, 4000, 100,
                        'legacy recovery evidence', %s, '{}'::json,
                        now() - interval '1 day', now() - interval '1 day'
                    )
                """,
                (
                    ledger_ids[0],
                    account_id,
                    merchant_id,
                    f'legacy-funding-{account_id}',
                    ledger_ids[1],
                    account_id,
                    merchant_id,
                    deposit_id,
                    f'legacy-recovery-{deposit_id}',
                ),
            )
            db.commit()

        upgrade = _alembic(
            database_url,
            'upgrade',
            '0020_rolling_confirmation_flow',
        )
        assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr

        with psycopg.connect(database_url) as db:
            assert db.execute(
                'SELECT version_num FROM alembic_version'
            ).fetchone()[0] == '0020_rolling_confirmation_flow'
            transfer = db.execute(
                """
                SELECT amount_usdt, recovered_usdt, remaining_usdt,
                       status, source, tx_hash
                  FROM merchant_rolling_transfers
                 WHERE merchant_id = %s
                """,
                (merchant_id,),
            ).fetchone()
            assert transfer == (
                100,
                40,
                60,
                'confirmed',
                'legacy_migration',
                None,
            )
            assert db.execute(
                """
                SELECT count(*)
                  FROM merchant_rolling_transfers
                 WHERE merchant_id = %s
                """,
                (plain_merchant_id,),
            ).fetchone()[0] == 0
            assert db.execute(
                """
                SELECT principal_usdt, recovered_usdt, outstanding_usdt
                  FROM merchant_rolling_accounts
                 WHERE id = %s
                """,
                (account_id,),
            ).fetchone() == (100, 40, 60)
            assert db.execute(
                """
                SELECT eligibility_status, eligible_transfer_sequence,
                       eligibility_source
                  FROM merchant_rolling_allocations
                 WHERE id = %s
                """,
                (allocation_id,),
            ).fetchone() == ('eligible', 1, 'legacy_migration')
            assert db.execute(
                """
                SELECT entry_type, amount_usdt, amount_rub
                  FROM merchant_rolling_transfer_consumptions
                 WHERE rolling_allocation_id = %s
                """,
                (allocation_id,),
            ).fetchone() == ('recovery', 40, 4000)
            assert db.execute(
                """
                SELECT count(*), count(rolling_transfer_id)
                  FROM merchant_rolling_ledger_entries
                 WHERE merchant_id = %s
                """,
                (merchant_id,),
            ).fetchone() == (2, 0)
            assert db.execute(
                """
                SELECT available, frozen
                  FROM balances
                 WHERE merchant_id = %s
                """,
                (merchant_id,),
            ).fetchone() == (Decimal('123.45'), Decimal('6.78'))
            assert db.execute(
                "SELECT to_regclass('public.merchant_finance_profiles')"
            ).fetchone()[0] is None

            db.execute(
                """
                INSERT INTO merchant_rolling_transfers (
                    id, merchant_id, sequence_no, amount_usdt,
                    recovered_usdt, remaining_usdt, network,
                    destination_address, tx_hash, status, source, sent_at,
                    created_by, idempotency_key
                )
                VALUES (
                    %s, %s, 1, 10, 0, 0, 'TRC20',
                    'TTestOnlyMigrationDestination', %s,
                    'pending_confirmation', 'registered', now(), %s, %s
                )
                """,
                (
                    uuid.uuid4(),
                    plain_merchant_id,
                    f'migration-tx-{uuid.uuid4().hex}',
                    plain_owner_id,
                    f'migration-transfer-{uuid.uuid4().hex}',
                ),
            )
            db.commit()

        blocked = _alembic(
            database_url,
            'downgrade',
            '0019_ai_office_config',
        )
        assert blocked.returncode != 0
        assert (
            'refusing destructive Rolling downgrade after transfer registration'
            in blocked.stdout + blocked.stderr
        )
        with psycopg.connect(database_url) as db:
            assert db.execute(
                'SELECT version_num FROM alembic_version'
            ).fetchone()[0] == '0020_rolling_confirmation_flow'
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(
                    sql.Identifier(database_name)
                )
            )


def test_postgres_0020_classifies_backup_derived_legacy_allocations():
    admin_url, database_url, database_name = _database_urls()
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(
            sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database_name))
        )
    try:
        to_0019 = _alembic(
            database_url,
            'upgrade',
            '0019_ai_office_config',
        )
        assert to_0019.returncode == 0, to_0019.stdout + to_0019.stderr

        now = datetime.now(timezone.utc)
        owner_id = uuid.uuid4()
        merchant_id = uuid.uuid4()
        funded_owner_id = uuid.uuid4()
        funded_merchant_id = uuid.uuid4()
        trader_id = uuid.uuid4()
        teamlead_id = uuid.uuid4()
        zero_account_id = uuid.uuid4()
        funded_account_id = uuid.uuid4()
        with psycopg.connect(database_url) as db:
            db.execute(
                """
                INSERT INTO users (
                    id, email, password_hash, role, is_active, is_locked,
                    failed_login_count, twofa_enabled,
                    trader_balance, trader_hold
                )
                VALUES
                    (%s, %s, 'unused', 'merchant', true, false, 0, false, 0, 0),
                    (%s, %s, 'unused', 'merchant', true, false, 0, false, 0, 0),
                    (%s, %s, 'unused', 'trader', true, false, 0, false, 321, 12),
                    (%s, %s, 'unused', 'teamlead', true, false, 0, false, 0, 0)
                """,
                (
                    owner_id,
                    f'backup-derived-owner-{uuid.uuid4().hex}@example.test',
                    funded_owner_id,
                    f'backup-funded-owner-{uuid.uuid4().hex}@example.test',
                    trader_id,
                    f'backup-derived-trader-{uuid.uuid4().hex}@example.test',
                    teamlead_id,
                    f'backup-derived-teamlead-{uuid.uuid4().hex}@example.test',
                ),
            )
            db.execute(
                """
                INSERT INTO merchants (
                    id, owner_id, name, ip_whitelist, sandbox_mode
                )
                VALUES
                    (%s, %s, 'Backup-derived merchant', '[]'::json, true),
                    (%s, %s, 'Backup-funded merchant', '[]'::json, true)
                """,
                (
                    merchant_id,
                    owner_id,
                    funded_merchant_id,
                    funded_owner_id,
                ),
            )
            db.execute(
                """
                INSERT INTO balances (
                    id, merchant_id, currency, available, frozen
                )
                VALUES (%s, %s, 'RUB', 12345.67, 89.01)
                """,
                (uuid.uuid4(), merchant_id),
            )
            db.execute(
                """
                INSERT INTO teamlead_balances (
                    id, teamlead_id, available_rub, frozen_rub, debt_rub,
                    total_earned_rub, total_paid_rub
                )
                VALUES (%s, %s, 44, 5, 6, 70, 15)
                """,
                (uuid.uuid4(), teamlead_id),
            )
            db.execute(
                """
                INSERT INTO merchant_rolling_accounts (
                    id, merchant_id, principal_usdt, recovered_usdt,
                    outstanding_usdt, status, created_at, updated_at
                )
                VALUES
                    (%s, %s, 0, 0, 0, 'exhausted', %s, %s),
                    (%s, %s, 100, 60, 40, 'active', %s, %s)
                """,
                (
                    zero_account_id,
                    merchant_id,
                    now - timedelta(days=4),
                    now - timedelta(days=1),
                    funded_account_id,
                    funded_merchant_id,
                    now - timedelta(days=3),
                    now - timedelta(days=1),
                ),
            )

            pending_id, _ = _insert_legacy_allocation(
                db,
                merchant_id=merchant_id,
                account_id=zero_account_id,
                created_at=now - timedelta(days=2),
                allocation_status='pending',
                deposit_status='pending',
            )
            released_id, _ = _insert_legacy_allocation(
                db,
                merchant_id=merchant_id,
                account_id=zero_account_id,
                created_at=now - timedelta(days=2, hours=-1),
                allocation_status='released',
                deposit_status='failed',
            )
            settle_paid_id, _ = _insert_legacy_allocation(
                db,
                merchant_id=merchant_id,
                account_id=zero_account_id,
                created_at=now - timedelta(days=2, hours=-2),
                allocation_status='paid',
                deposit_status='paid',
                settle_credited_rub=Decimal('4000'),
            )
            before_funding_id, _ = _insert_legacy_allocation(
                db,
                merchant_id=funded_merchant_id,
                account_id=funded_account_id,
                created_at=now - timedelta(days=4),
                allocation_status='released',
                deposit_status='failed',
            )
            rolling_id, rolling_deposit_id = _insert_legacy_allocation(
                db,
                merchant_id=funded_merchant_id,
                account_id=funded_account_id,
                created_at=now - timedelta(days=1),
                allocation_status='paid',
                deposit_status='paid',
                rolling_applied_usdt=Decimal('40'),
                rolling_applied_rub=Decimal('4000'),
            )
            crossing_id, crossing_deposit_id = _insert_legacy_allocation(
                db,
                merchant_id=funded_merchant_id,
                account_id=funded_account_id,
                created_at=now - timedelta(hours=12),
                allocation_status='paid',
                deposit_status='paid',
                rolling_applied_usdt=Decimal('20'),
                rolling_applied_rub=Decimal('2000'),
                settle_credited_rub=Decimal('2000'),
            )
            db.execute(
                """
                INSERT INTO merchant_rolling_ledger_entries (
                    id, rolling_account_id, merchant_id, deposit_id,
                    entry_type, amount_usdt, amount_rub, rate_rub,
                    funded_at, reason, idempotency_key, metadata_json,
                    created_at, updated_at
                )
                VALUES
                    (
                        %s, %s, %s, NULL, 'funding', 100, NULL, NULL,
                        %s, 'funding evidence', %s, '{}'::json, %s, %s
                    ),
                    (
                        %s, %s, %s, %s, 'recovery', 40, 4000, 100,
                        NULL, 'recovery evidence', %s, '{}'::json, %s, %s
                    ),
                    (
                        %s, %s, %s, %s, 'recovery', 20, 2000, 100,
                        NULL, 'crossing evidence', %s, '{}'::json, %s, %s
                    )
                """,
                (
                    uuid.uuid4(),
                    funded_account_id,
                    funded_merchant_id,
                    now - timedelta(days=3),
                    f'fixture-funding-{uuid.uuid4().hex}',
                    now - timedelta(days=3),
                    now - timedelta(days=3),
                    uuid.uuid4(),
                    funded_account_id,
                    funded_merchant_id,
                    rolling_deposit_id,
                    f'fixture-recovery-{uuid.uuid4().hex}',
                    now - timedelta(days=1),
                    now - timedelta(days=1),
                    uuid.uuid4(),
                    funded_account_id,
                    funded_merchant_id,
                    crossing_deposit_id,
                    f'fixture-crossing-{uuid.uuid4().hex}',
                    now - timedelta(hours=12),
                    now - timedelta(hours=12),
                ),
            )
            before_finance = {
                'merchant': db.execute(
                    'SELECT available, frozen FROM balances WHERE merchant_id=%s',
                    (merchant_id,),
                ).fetchone(),
                'trader': db.execute(
                    'SELECT trader_balance, trader_hold FROM users WHERE id=%s',
                    (trader_id,),
                ).fetchone(),
                'teamlead': db.execute(
                    """
                    SELECT available_rub, frozen_rub, debt_rub,
                           total_earned_rub, total_paid_rub
                      FROM teamlead_balances
                     WHERE teamlead_id=%s
                    """,
                    (teamlead_id,),
                ).fetchone(),
                'rolling': db.execute(
                    """
                    SELECT sum(principal_usdt), sum(recovered_usdt),
                           sum(outstanding_usdt)
                      FROM merchant_rolling_accounts
                    """
                ).fetchone(),
                'history': db.execute(
                    """
                    SELECT
                        (SELECT count(*) FROM merchant_rolling_allocations),
                        (SELECT count(*) FROM merchant_rolling_ledger_entries)
                    """
                ).fetchone(),
            }
            db.commit()

        upgrade = _alembic(
            database_url,
            'upgrade',
            '0020_rolling_confirmation_flow',
        )
        assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr

        with psycopg.connect(database_url) as db:
            ineligible_rows = {
                row[0]: row[1:]
                for row in db.execute(
                    """
                    SELECT id, eligibility_status,
                           eligible_transfer_sequence,
                           rolling_eligible_at, eligibility_source,
                           rolling_applied_usdt, rolling_applied_rub,
                           settle_credited_rub, status
                      FROM merchant_rolling_allocations
                     WHERE id = ANY(%s)
                    """,
                    (
                        [
                            pending_id,
                            released_id,
                            settle_paid_id,
                            before_funding_id,
                        ],
                    ),
                ).fetchall()
            }
            for allocation_id in (
                pending_id,
                released_id,
                settle_paid_id,
                before_funding_id,
            ):
                row = ineligible_rows[allocation_id]
                assert row[0:4] == (
                    'ineligible',
                    None,
                    None,
                    'legacy_no_confirmed_funding',
                )
            assert ineligible_rows[pending_id][7] == 'pending'
            assert ineligible_rows[released_id][7] == 'released'
            assert ineligible_rows[settle_paid_id][4:8] == (
                Decimal('0.000000'),
                Decimal('0.00'),
                Decimal('4000.00'),
                'paid',
            )
            assert db.execute(
                """
                SELECT eligibility_status, eligible_transfer_sequence,
                       eligibility_source
                  FROM merchant_rolling_allocations
                 WHERE id = ANY(%s)
                 ORDER BY id
                """,
                ([rolling_id, crossing_id],),
            ).fetchall() == [
                ('eligible', 1, 'legacy_migration'),
                ('eligible', 1, 'legacy_migration'),
            ]
            assert db.execute(
                """
                SELECT amount_usdt, recovered_usdt, remaining_usdt
                  FROM merchant_rolling_transfers
                 WHERE rolling_account_id=%s
                """,
                (funded_account_id,),
            ).fetchone() == (
                Decimal('100.000000'),
                Decimal('60.000000'),
                Decimal('40.000000'),
            )
            assert db.execute(
                """
                SELECT count(*), sum(amount_usdt), sum(amount_rub)
                  FROM merchant_rolling_transfer_consumptions
                 WHERE rolling_account_id=%s AND entry_type='recovery'
                """,
                (funded_account_id,),
            ).fetchone() == (
                2,
                Decimal('60.000000'),
                Decimal('6000.00'),
            )
            after_finance = {
                'merchant': db.execute(
                    'SELECT available, frozen FROM balances WHERE merchant_id=%s',
                    (merchant_id,),
                ).fetchone(),
                'trader': db.execute(
                    'SELECT trader_balance, trader_hold FROM users WHERE id=%s',
                    (trader_id,),
                ).fetchone(),
                'teamlead': db.execute(
                    """
                    SELECT available_rub, frozen_rub, debt_rub,
                           total_earned_rub, total_paid_rub
                      FROM teamlead_balances
                     WHERE teamlead_id=%s
                    """,
                    (teamlead_id,),
                ).fetchone(),
                'rolling': db.execute(
                    """
                    SELECT sum(principal_usdt), sum(recovered_usdt),
                           sum(outstanding_usdt)
                      FROM merchant_rolling_accounts
                    """
                ).fetchone(),
                'history': db.execute(
                    """
                    SELECT
                        (SELECT count(*) FROM merchant_rolling_allocations),
                        (SELECT count(*) FROM merchant_rolling_ledger_entries)
                    """
                ).fetchone(),
            }
            assert after_finance == before_finance
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(
                    sql.Identifier(database_name)
                )
            )


def test_postgres_0020_rejects_consumption_without_provable_funding():
    admin_url, database_url, database_name = _database_urls()
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(
            sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database_name))
        )
    try:
        to_0019 = _alembic(
            database_url,
            'upgrade',
            '0019_ai_office_config',
        )
        assert to_0019.returncode == 0, to_0019.stdout + to_0019.stderr
        owner_id = uuid.uuid4()
        merchant_id = uuid.uuid4()
        account_id = uuid.uuid4()
        with psycopg.connect(database_url) as db:
            db.execute(
                """
                INSERT INTO users (
                    id, email, password_hash, role, is_active, is_locked,
                    failed_login_count, twofa_enabled
                )
                VALUES (%s, %s, 'unused', 'merchant', true, false, 0, false)
                """,
                (
                    owner_id,
                    f'unfunded-consumption-{uuid.uuid4().hex}@example.test',
                ),
            )
            db.execute(
                """
                INSERT INTO merchants (
                    id, owner_id, name, ip_whitelist, sandbox_mode
                )
                VALUES (%s, %s, 'Unfunded consumption', '[]'::json, true)
                """,
                (merchant_id, owner_id),
            )
            db.execute(
                """
                INSERT INTO merchant_rolling_accounts (
                    id, merchant_id, principal_usdt, recovered_usdt,
                    outstanding_usdt, status
                )
                VALUES (%s, %s, 0, 0, 0, 'exhausted')
                """,
                (account_id, merchant_id),
            )
            _insert_legacy_allocation(
                db,
                merchant_id=merchant_id,
                account_id=account_id,
                created_at=datetime.now(timezone.utc) - timedelta(days=1),
                allocation_status='reversed',
                deposit_status='paid',
                rolling_applied_usdt=Decimal('10'),
                rolling_applied_rub=Decimal('1000'),
                settle_credited_rub=Decimal('3000'),
            )
            db.commit()

        blocked = _alembic(
            database_url,
            'upgrade',
            '0020_rolling_confirmation_flow',
        )
        assert blocked.returncode != 0
        output = blocked.stdout + blocked.stderr
        assert (
            'rolling migration invariant failed: '
            'proven consumption lacks legacy funding'
        ) in output
        assert str(owner_id) not in output
        assert str(merchant_id) not in output
        with psycopg.connect(database_url) as db:
            assert db.execute(
                'SELECT version_num FROM alembic_version'
            ).fetchone()[0] == '0019_ai_office_config'
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(
                    sql.Identifier(database_name)
                )
            )


def test_postgres_0020_stops_on_contradictory_legacy_financial_totals():
    admin_url, database_url, database_name = _database_urls()
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(
            sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database_name))
        )
    try:
        to_0019 = _alembic(
            database_url,
            'upgrade',
            '0019_ai_office_config',
        )
        assert to_0019.returncode == 0, to_0019.stdout + to_0019.stderr
        owner_id = uuid.uuid4()
        merchant_id = uuid.uuid4()
        with psycopg.connect(database_url) as db:
            db.execute(
                """
                INSERT INTO users (
                    id, email, password_hash, role, is_active, is_locked,
                    failed_login_count, twofa_enabled
                )
                VALUES (%s, %s, 'unused', 'merchant', true, false, 0, false)
                """,
                (
                    owner_id,
                    f'broken-rolling-{uuid.uuid4().hex}@example.test',
                ),
            )
            db.execute(
                """
                INSERT INTO merchants (
                    id, owner_id, name, ip_whitelist, sandbox_mode
                )
                VALUES (
                    %s, %s, 'Broken totals merchant', '[]'::json, true
                )
                """,
                (merchant_id, owner_id),
            )
            db.execute(
                """
                INSERT INTO merchant_rolling_accounts (
                    id, merchant_id, principal_usdt, recovered_usdt,
                    outstanding_usdt, status
                )
                VALUES (%s, %s, 100, 30, 60, 'active')
                """,
                (uuid.uuid4(), merchant_id),
            )
            db.commit()

        blocked = _alembic(
            database_url,
            'upgrade',
            '0020_rolling_confirmation_flow',
        )
        assert blocked.returncode != 0
        output = blocked.stdout + blocked.stderr
        assert (
            'rolling migration invariant failed: account aggregate mismatch'
            in output
        )
        assert str(owner_id) not in output
        assert str(merchant_id) not in output
        with psycopg.connect(database_url) as db:
            assert db.execute(
                'SELECT version_num FROM alembic_version'
            ).fetchone()[0] == '0019_ai_office_config'
            assert db.execute(
                "SELECT to_regclass('public.merchant_rolling_transfers')"
            ).fetchone()[0] is None
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(
                    sql.Identifier(database_name)
                )
            )


def test_postgres_0023_normalizes_attempts_and_guards_signing_key_downgrade():
    admin_url, database_url, database_name = _database_urls()
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(
            sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database_name))
        )
    try:
        to_0022 = _alembic(
            database_url,
            'upgrade',
            '0022_hmac_v2_idempotency',
        )
        assert to_0022.returncode == 0, to_0022.stdout + to_0022.stderr

        user_id = uuid.uuid4()
        merchant_id = uuid.uuid4()
        event_id = uuid.uuid4()
        with psycopg.connect(database_url) as db:
            db.execute(
                """
                INSERT INTO users (
                    id, email, password_hash, role, is_active, is_locked,
                    failed_login_count, twofa_enabled
                )
                VALUES (%s, %s, 'unused', 'merchant', true, false, 0, false)
                """,
                (
                    user_id,
                    f'webhook-migration-{uuid.uuid4().hex}@example.test',
                ),
            )
            db.execute(
                """
                INSERT INTO merchants (
                    id, owner_id, name, ip_whitelist, sandbox_mode
                )
                VALUES (%s, %s, 'Webhook migration merchant', '[]'::json, true)
                """,
                (merchant_id, user_id),
            )
            db.execute(
                """
                INSERT INTO webhook_events (
                    id, merchant_id, event_type, payload, status, attempts
                )
                VALUES (%s, %s, 'deposit.paid', '{}'::json, 'queued', 0)
                """,
                (event_id, merchant_id),
            )
            for created_at in (
                datetime.now(timezone.utc) - timedelta(minutes=2),
                datetime.now(timezone.utc) - timedelta(minutes=1),
            ):
                db.execute(
                    """
                    INSERT INTO webhook_delivery_attempts (
                        id, webhook_event_id, attempt_no, status,
                        created_at, updated_at
                    )
                    VALUES (%s, %s, 1, 'failed', %s, %s)
                    """,
                    (uuid.uuid4(), event_id, created_at, created_at),
                )
            db.commit()

        to_0023 = _alembic(
            database_url,
            'upgrade',
            '0023_webhook_hardening',
        )
        assert to_0023.returncode == 0, to_0023.stdout + to_0023.stderr

        with psycopg.connect(database_url) as db:
            assert db.execute(
                """
                SELECT attempt_no
                  FROM webhook_delivery_attempts
                 WHERE webhook_event_id = %s
                 ORDER BY attempt_no
                """,
                (event_id,),
            ).fetchall() == [(1,), (2,)]
            assert db.execute(
                """
                SELECT attempts, max_attempts
                  FROM webhook_events
                 WHERE id = %s
                """,
                (event_id,),
            ).fetchone() == (2, 5)
            db.execute(
                """
                INSERT INTO merchant_webhook_signing_keys (
                    id, merchant_id, key_id, encrypted_secret,
                    status, created_by
                )
                VALUES (%s, %s, %s, %s, 'active', %s)
                """,
                (
                    uuid.uuid4(),
                    merchant_id,
                    f'whk_{uuid.uuid4().hex}',
                    encrypt_secret('webhook-downgrade-guard-secret'),
                    user_id,
                ),
            )
            db.commit()

        blocked = _alembic(
            database_url,
            'downgrade',
            '0022_hmac_v2_idempotency',
        )
        assert blocked.returncode != 0
        assert (
            'webhook hardening downgrade blocked: signing keys exist'
            in blocked.stdout + blocked.stderr
        )
        with psycopg.connect(database_url) as db:
            assert db.execute(
                'SELECT version_num FROM alembic_version'
            ).fetchone()[0] == '0023_webhook_hardening'
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(
                    sql.Identifier(database_name)
                )
            )


def test_postgres_0025_preserves_existing_teamlead_finance_and_guards_activity():
    admin_url, database_url, database_name = _database_urls()
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(
            sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database_name))
        )
    try:
        to_0024 = _alembic(
            database_url,
            'upgrade',
            '0024_required_schema_indexes',
        )
        assert to_0024.returncode == 0, to_0024.stdout + to_0024.stderr
        teamlead_id = uuid.uuid4()
        trader_id = uuid.uuid4()
        owner_id = uuid.uuid4()
        merchant_id = uuid.uuid4()
        assignment_id = uuid.uuid4()
        balance_id = uuid.uuid4()
        assigned_at = datetime.now(timezone.utc) - timedelta(days=30)
        with psycopg.connect(database_url) as db:
            for user_id, role, label in (
                (teamlead_id, 'teamlead', 'teamlead'),
                (trader_id, 'trader', 'trader'),
                (owner_id, 'merchant', 'merchant'),
            ):
                db.execute(
                    """
                    INSERT INTO users (
                        id, email, password_hash, role, is_active,
                        is_locked, failed_login_count, twofa_enabled
                    )
                    VALUES (%s, %s, 'unused', %s, true, false, 0, false)
                    """,
                    (
                        user_id,
                        f'0025-{label}-{uuid.uuid4().hex}@example.test',
                        role,
                    ),
                )
            db.execute(
                """
                INSERT INTO merchants (
                    id, owner_id, name, ip_whitelist, sandbox_mode
                )
                VALUES (%s, %s, '0025 clone merchant', '[]'::json, true)
                """,
                (merchant_id, owner_id),
            )
            db.execute(
                """
                INSERT INTO teamlead_trader_assignments (
                    id, teamlead_id, trader_id, commission_percent,
                    effective_from, created_by, creation_reason
                )
                VALUES (%s, %s, %s, 0.75, %s, %s, 'existing assignment')
                """,
                (
                    assignment_id,
                    teamlead_id,
                    trader_id,
                    assigned_at,
                    teamlead_id,
                ),
            )
            db.execute(
                """
                INSERT INTO teamlead_balances (
                    id, teamlead_id, available_rub, frozen_rub,
                    debt_rub, total_earned_rub, total_paid_rub
                )
                VALUES (%s, %s, 123.45, 67.89, 10.00, 500.00, 298.66)
                """,
                (balance_id, teamlead_id),
            )
            before = db.execute(
                """
                SELECT
                    (SELECT count(*) FROM teamlead_trader_assignments),
                    (SELECT coalesce(sum(available_rub), 0)
                       FROM teamlead_balances),
                    (SELECT coalesce(sum(frozen_rub), 0)
                       FROM teamlead_balances),
                    (SELECT coalesce(sum(debt_rub), 0)
                       FROM teamlead_balances),
                    (SELECT coalesce(sum(total_earned_rub), 0)
                       FROM teamlead_balances),
                    (SELECT coalesce(sum(total_paid_rub), 0)
                       FROM teamlead_balances),
                    (SELECT count(*) FROM teamlead_ledger_entries)
                """
            ).fetchone()
            db.commit()

        upgrade = _alembic(database_url, 'upgrade', 'head')
        assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr
        with psycopg.connect(database_url) as db:
            assert db.execute(
                'SELECT version_num FROM alembic_version'
            ).fetchone()[0] == '0027_aggregator_credentials'
            after = db.execute(
                """
                SELECT
                    (SELECT count(*) FROM teamlead_trader_assignments),
                    (SELECT coalesce(sum(available_rub), 0)
                       FROM teamlead_balances),
                    (SELECT coalesce(sum(frozen_rub), 0)
                       FROM teamlead_balances),
                    (SELECT coalesce(sum(debt_rub), 0)
                       FROM teamlead_balances),
                    (SELECT coalesce(sum(total_earned_rub), 0)
                       FROM teamlead_balances),
                    (SELECT coalesce(sum(total_paid_rub), 0)
                       FROM teamlead_balances),
                    (SELECT count(*) FROM teamlead_ledger_entries)
                """
            ).fetchone()
            assert after == before
            assert db.execute(
                'SELECT count(*) FROM teamlead_merchant_assignments'
            ).fetchone()[0] == 0
            assert db.execute(
                'SELECT count(*) FROM teamlead_merchant_accruals'
            ).fetchone()[0] == 0

        clean_downgrade = _alembic(
            database_url,
            'downgrade',
            '0024_required_schema_indexes',
        )
        assert clean_downgrade.returncode == 0, (
            clean_downgrade.stdout + clean_downgrade.stderr
        )
        reupgrade = _alembic(database_url, 'upgrade', 'head')
        assert reupgrade.returncode == 0, reupgrade.stdout + reupgrade.stderr
        with psycopg.connect(database_url) as db:
            db.execute(
                """
                INSERT INTO teamlead_merchant_assignments (
                    id, teamlead_id, merchant_id, commission_percent,
                    valid_from, created_by, reason
                )
                VALUES (%s, %s, %s, 1.25, %s, %s, 'downgrade guard')
                """,
                (
                    uuid.uuid4(),
                    teamlead_id,
                    merchant_id,
                    datetime.now(timezone.utc),
                    teamlead_id,
                ),
            )
            db.commit()
        blocked = _alembic(
            database_url,
            'downgrade',
            '0024_required_schema_indexes',
        )
        assert blocked.returncode != 0
        assert (
            'refusing TeamLead merchant referral downgrade after activity'
            in blocked.stdout + blocked.stderr
        )
        with psycopg.connect(database_url) as db:
            assert db.execute(
                'SELECT version_num FROM alembic_version'
            ).fetchone()[0] == '0027_aggregator_credentials'
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(
                    sql.Identifier(database_name)
                )
            )
