"""Run existing project commands against guarded, local recovery environments.

Examples: python -m scripts.recovery_run migrate|bootstrap|seed|test|smoke
"""
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
LOCAL = Path(os.getenv('TRADESPACE_LOCAL_DIR', str(ROOT.parent / 'TradeSpace-local')))
if LOCAL.resolve().is_relative_to(ROOT.resolve()):
    raise RuntimeError('Local credentials must be outside the repository')


def local_environment(test=False):
    values = dotenv_values(LOCAL / ('.env.test' if test else '.env'))
    expected = 'tradespace_test' if test else 'tradespace_dev'
    for key in ('DATABASE_URL', 'SYNC_DATABASE_URL'):
        url = urlsplit(values.get(key, ''))
        if url.hostname != '127.0.0.1' or url.port != 55432 or url.path != '/' + expected:
            raise SystemExit(f'{key} must point to the isolated local {expected} database.')
    for key in ('REDIS_URL', 'CELERY_BROKER_URL', 'CELERY_RESULT_BACKEND'):
        url = urlsplit(values.get(key, ''))
        if url.hostname != '127.0.0.1' or url.port != 56379:
            raise SystemExit(f'{key} must point to the local recovery Redis.')
    if values.get('ENV') not in ('local', 'test'):
        raise SystemExit('Only local/test environments are allowed.')
    return {**os.environ, **{k: v for k, v in values.items() if v is not None},
            'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1',
            'PATH': str(ROOT / 'tmp/runtime/postgresql/pgsql/bin') + os.pathsep + os.environ.get('PATH','')}


def run(args, env):
    subprocess.run([sys.executable, '-m', *args], cwd=ROOT, env=env, check=True)


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else 'smoke'
    env = local_environment(test=action == 'test')
    if action == 'databases':
        import psycopg
        from psycopg import sql
        url = urlsplit(env['SYNC_DATABASE_URL'])
        with psycopg.connect(host='127.0.0.1', port=55432, user=url.username,
                             password=url.password, dbname='postgres', autocommit=True) as db:
            for name in ('tradespace_dev', 'tradespace_test'):
                if not db.execute('SELECT 1 FROM pg_database WHERE datname=%s', (name,)).fetchone():
                    db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
                    print('Created', name)
    elif action == 'migrate':
        run(['alembic', 'upgrade', 'head'], env)
        run(['alembic', 'current'], env)
    elif action == 'bootstrap':
        run(['scripts.bootstrap_superadmin'], env)
    elif action == 'seed':
        run(['scripts.seed'], {**env, 'SEED_DEMO_DATA': 'true'})
    elif action == 'test':
        if env.get('TEST_DATABASE_URL') != env['DATABASE_URL']:
            raise SystemExit('Test URL mismatch')
        run(['alembic', 'upgrade', 'head'], env)
        run(['pytest', '-q', '-p', 'no:cacheprovider',
             '--junitxml=test-results/recovery-pytest.xml', *sys.argv[2:]], env)
    elif action == 'smoke':
        run(['scripts.smoke_v2', '--base-url', 'http://127.0.0.1:8000'], env)
    elif action == 'preflight':
        run(['scripts.preflight_v2', '--base-url', 'http://127.0.0.1:8000'], env)
    else:
        raise SystemExit('Use databases, migrate, bootstrap, seed, test, smoke, preflight')


if __name__ == '__main__':
    main()
