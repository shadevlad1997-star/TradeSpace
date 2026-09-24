"""Back up and archive ONLY the guarded native local dev/test databases.

Stop application services first. This never drops a database or flushes Redis.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
from urllib.parse import urlsplit
from datetime import datetime,timezone
import psycopg
from psycopg import sql
from cryptography.fernet import Fernet
from dotenv import dotenv_values
from scripts.recovery_run import ROOT,LOCAL,local_environment


def reset(archive: Path):
    env=local_environment();local_environment(test=True)
    archive=archive.resolve()
    if archive.is_relative_to(ROOT.resolve()) or archive.is_relative_to(LOCAL.resolve()):
        raise RuntimeError('Backup must be outside repository and active configuration')
    for port in (8000,56379):
        try:
            with socket.create_connection(('127.0.0.1',port),timeout=1):pass
        except OSError:continue
        raise RuntimeError('Stop project API, worker, beat, receiver and Redis first')
    u=urlsplit(env['SYNC_DATABASE_URL']);cipher=Fernet(env['ENCRYPTION_KEY'].encode())
    pg=ROOT/'tmp/runtime/postgresql/pgsql/bin';stamp=datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
    archive.mkdir(parents=True,exist_ok=False);report={}
    # Preserve decryption configuration separately from release artifacts.
    shutil.copytree(LOCAL,archive/'local-config-before')
    with psycopg.connect(host=u.hostname,port=u.port,user=u.username,password=u.password,dbname='postgres',autocommit=True,connect_timeout=5) as db:
        for name in ('tradespace_dev','tradespace_test'):
            owner=db.execute('SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=%s',(name,)).fetchone()
            if not owner or owner[0]!=u.username:raise RuntimeError('Database owner not confirmed')
            if db.execute('SELECT 1 FROM pg_stat_activity WHERE datname=%s',(name,)).fetchone():raise RuntimeError('Active database clients remain')
        # Both backups must verify BEFORE either database is archived.
        for name in ('tradespace_dev','tradespace_test'):
            result=subprocess.run([str(pg/'pg_dump.exe'),'-h',u.hostname,'-p',str(u.port),'-U',u.username,'-d',name,'-Fc','--no-owner','--no-privileges'],env={**os.environ,'PGPASSWORD':u.password},capture_output=True,check=True,timeout=180)
            path=archive/(name+'.dump.fernet');path.write_bytes(cipher.encrypt(result.stdout));plain=cipher.decrypt(path.read_bytes());assert plain==result.stdout
            subprocess.run([str(pg/'pg_restore.exe'),'--list'],input=plain,capture_output=True,check=True,timeout=30)
            report[name]={'sha256':hashlib.sha256(plain).hexdigest(),'verified':True,'archived_name':name+'_archive_'+stamp}
        for name,item in report.items():
            db.execute(sql.SQL('ALTER DATABASE {} RENAME TO {}').format(sql.Identifier(name),sql.Identifier(item['archived_name'])))
            db.execute(sql.SQL('ALTER DATABASE {} ALLOW_CONNECTIONS false').format(sql.Identifier(item['archived_name'])))
            db.execute(sql.SQL('CREATE DATABASE {} OWNER {}').format(sql.Identifier(name),sql.Identifier(u.username)))
    key=Fernet.generate_key().decode();session=secrets.token_urlsafe(48);redis_password=secrets.token_urlsafe(32)
    import pyotp
    for filename in ('.env','.env.test'):
        values=dict(dotenv_values(LOCAL/filename));values.update(ENCRYPTION_KEY=key,SECRET_KEY=session,REDIS_PASSWORD=redis_password)
        for field in values:
            if field.endswith('_PASSWORD') and field not in {'POSTGRES_PASSWORD','REDIS_PASSWORD'}:values[field]=secrets.token_urlsafe(24)
            elif field.endswith('_2FA_SECRET'):values[field]=pyotp.random_base32()
            elif field.startswith('DEMO_') and ('SECRET' in field or 'API_KEY' in field):values[field]=secrets.token_urlsafe(32)
        for field in ('REDIS_URL','CELERY_BROKER_URL','CELERY_RESULT_BACKEND'):
            previous=urlsplit(values[field]);values[field]=f'redis://:{redis_password}@127.0.0.1:56379'+previous.path
        (LOCAL/filename).write_text('\n'.join(f'{k}={v}' for k,v in values.items() if v is not None)+'\n',encoding='utf8')
    for filename in ('qa-access.json','qa-scenarios.json'):
        p=LOCAL/filename
        if p.exists():p.unlink() # exact local files, already backed up above
    run=LOCAL/'run'
    if run.exists():
        for p in run.iterdir():
            if p.is_file():p.unlink() # no recursion; complete copy preserved above
    run.mkdir(exist_ok=True)
    data=Path(env['TRADESPACE_DATA_DIR'])/('redis-'+stamp);data.mkdir(exist_ok=False)
    posix='/cygdrive/'+data.drive[0].lower()+data.as_posix()[2:]
    (run/'redis.conf').write_text(f'bind 127.0.0.1\nprotected-mode yes\nport 56379\nrequirepass {redis_password}\ndir {posix}\nappendonly yes\nappendfsync everysec\nsave 60 1\n',encoding='ascii')
    (archive/'reset-result.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    print('Local DBs archived, new empty DBs created, Redis isolated. Run migrate/bootstrap, then optional QA seed.')


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--confirm-local-reset',action='store_true');parser.add_argument('--backup-dir',type=Path,required=True);args=parser.parse_args()
    if not args.confirm_local_reset:raise SystemExit('Explicit --confirm-local-reset is required')
    reset(args.backup_dir)

if __name__=='__main__':main()
