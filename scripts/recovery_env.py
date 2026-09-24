"""Generate new, untracked credentials for this local recovery only."""
import base64
from pathlib import Path
import secrets

from scripts.recovery_run import ROOT, LOCAL


def main():
    LOCAL.mkdir(parents=True, exist_ok=True)
    if (LOCAL / '.env').exists() or (LOCAL / '.env.test').exists():
        raise SystemExit('Environment already exists; refusing to replace encryption keys.')
    values = {
        'APP_NAME': 'TradeSpace', 'ENV': 'local', 'DEBUG': 'true',
        'TRADESPACE_DATA_DIR': (ROOT.parent / 'TradeSpaceData').as_posix(),
        'SECRET_KEY': secrets.token_urlsafe(48),
        'ENCRYPTION_KEY': base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        'POSTGRES_USER': 'tradespace_local', 'POSTGRES_PASSWORD': secrets.token_hex(24),
        'POSTGRES_DB': 'tradespace_dev', 'REDIS_PASSWORD': secrets.token_hex(24),
        'SUPERADMIN_EMAIL': 'superadmin@tradespace.local',
        'SUPERADMIN_PASSWORD': secrets.token_urlsafe(24),
        'SUPERADMIN_2FA_SECRET': base64.b32encode(secrets.token_bytes(20)).decode(),
        'METRICS_TOKEN': secrets.token_urlsafe(32),
        'CORS_ORIGINS': 'http://localhost:8000,http://127.0.0.1:8000',
        'CSRF_TRUSTED_ORIGINS': 'http://localhost:8000,http://127.0.0.1:8000',
        'TRUSTED_HOSTS': 'localhost,127.0.0.1', 'TRUSTED_PROXY_IPS': '127.0.0.1,::1',
        'SEED_DEMO_DATA': 'false', 'DOCS_ENABLED': 'false', 'OPENAPI_ENABLED': 'false',
        'HSTS_ENABLED': 'false', 'RAPIRA_RATES_ENABLED': 'true',
        'TRADESPACE_UI_ENABLED': 'true',
        'RAPIRA_RATES_URL': 'https://api.rapira.net/open/market/rates',
    }
    for role in ('ADMIN', 'SUPPORT', 'OPERATOR', 'MERCHANT'):
        values[f'DEMO_{role}_EMAIL'] = f'{role.lower()}@tradespace.local'
        values[f'DEMO_{role}_PASSWORD'] = secrets.token_urlsafe(24)
        if role in ('ADMIN', 'SUPPORT'):
            values[f'DEMO_{role}_2FA_SECRET'] = base64.b32encode(secrets.token_bytes(20)).decode()
    values['DEMO_MERCHANT_API_KEY'] = 'pk_recovery_' + secrets.token_urlsafe(18)
    values['DEMO_MERCHANT_SECRET'] = secrets.token_urlsafe(32)
    pg = f"{values['POSTGRES_USER']}:{values['POSTGRES_PASSWORD']}@127.0.0.1:55432"
    values['DATABASE_URL'] = f'postgresql+asyncpg://{pg}/tradespace_dev'
    values['SYNC_DATABASE_URL'] = f'postgresql+psycopg://{pg}/tradespace_dev'
    redis = f"redis://:{values['REDIS_PASSWORD']}@127.0.0.1:56379"
    for name, index in [('REDIS_URL', 0), ('CELERY_BROKER_URL', 1), ('CELERY_RESULT_BACKEND', 2)]:
        values[name] = f'{redis}/{index}'
    (LOCAL / '.env').write_text('# LOCAL ONLY: generated credentials, never commit.\n' +
                              '\n'.join(f'{k}={v}' for k, v in values.items()) + '\n', encoding='utf-8')
    test = dict(values, ENV='test', DEBUG='false', RAPIRA_RATES_ENABLED='false',
                POSTGRES_DB='tradespace_test',
                DATABASE_URL=f'postgresql+asyncpg://{pg}/tradespace_test',
                SYNC_DATABASE_URL=f'postgresql+psycopg://{pg}/tradespace_test',
                TEST_DATABASE_URL=f'postgresql+asyncpg://{pg}/tradespace_test',
                REDIS_URL=f'{redis}/10', CELERY_BROKER_URL=f'{redis}/11',
                CELERY_RESULT_BACKEND=f'{redis}/12')
    (LOCAL / '.env.test').write_text('# DISPOSABLE TEST DATABASE ONLY.\n' +
                                   '\n'.join(f'{k}={v}' for k, v in test.items()) + '\n', encoding='utf-8')
    run = LOCAL / 'run'
    run.mkdir(exist_ok=True)
    (run / 'postgres-password.txt').write_text(values['POSTGRES_PASSWORD'] + '\n', encoding='ascii')
    redis_dir = (Path(values['TRADESPACE_DATA_DIR']) / 'redis').as_posix()
    Path(redis_dir).mkdir(parents=True, exist_ok=True)
    (run / 'redis.conf').write_text(
        'bind 127.0.0.1\nprotected-mode yes\nport 56379\n' +
        f"requirepass {values['REDIS_PASSWORD']}\ndir /cygdrive/{redis_dir[0].lower()}{redis_dir[2:]}\n" +
        'appendonly yes\nappendfsync everysec\nsave 60 1\n', encoding='utf-8')
    print('Generated .env, .env.test and local runtime configuration; no secrets printed.')


if __name__ == '__main__':
    main()
