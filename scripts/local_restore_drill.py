"""Local-only encrypted snapshot and ordinary transactional restore drill.
No source data is updated; disposable database names have an audit prefix.
"""
import hashlib, json, os, subprocess, sys, time, uuid
from pathlib import Path
from urllib.parse import urlsplit
import psycopg
from psycopg import sql
from cryptography.fernet import Fernet
from scripts.recovery_run import local_environment,ROOT
import argparse
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--confirm-local',action='store_true')
parser.add_argument('--output-dir',type=Path,required=True)
args=parser.parse_args()
if not args.confirm_local:raise SystemExit('Explicit --confirm-local is required')
OUT=args.output_dir.resolve()
if OUT.is_relative_to(ROOT.resolve()):raise SystemExit('Backup directory must be outside repository')
OUT.mkdir(parents=True,exist_ok=False)
PG=ROOT/'tmp/runtime/postgresql/pgsql/bin'
env=local_environment(); u=urlsplit(env['SYNC_DATABASE_URL'])
cipher=Fernet(env['ENCRYPTION_KEY'].encode());report={}
def connect(name,autocommit=False):
    return psycopg.connect(host='127.0.0.1',port=55432,user=u.username,password=u.password,dbname=name,autocommit=autocommit)
def create(name):
    assert name.startswith('tradespace_audit_')
    with connect('postgres',True) as db:
        db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
def hashes(db):
    result={}
    for (name,) in db.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename").fetchall():
        data=db.execute(sql.SQL('SELECT row_to_json(t)::text FROM {} t').format(sql.Identifier(name))).fetchall()
        result[name]={'rows':len(data),'sha256':hashlib.sha256('\n'.join(sorted(r[0] for r in data)).encode()).hexdigest()}
    return result
def money_totals(db):
    totals={}
    tables={'balances','ledger_entries','trader_ledger_entries','merchant_settlements','teamlead_ledger_entries','teamlead_balances','merchant_rolling_transfers','merchant_rolling_ledger_entries','deposits','payouts'}
    for name,column in db.execute("SELECT table_name,column_name FROM information_schema.columns WHERE table_schema='public' AND data_type='numeric' ORDER BY table_name,column_name").fetchall():
        if name in tables:
            amount=db.execute(sql.SQL('SELECT coalesce(sum({}),0) FROM {}').format(sql.Identifier(column),sql.Identifier(name))).fetchone()[0]
            totals[name+'.'+column]=str(amount)
    return totals

def tool(exe,db,args=(),data=None):
    return subprocess.run([str(PG/exe),'-h','127.0.0.1','-p','55432','-U',u.username,'-d',db,*args],input=data,capture_output=True,env={**os.environ,'PGPASSWORD':u.password},timeout=180)
def drill(source,label):
    start=time.monotonic()
    with connect(source) as db:
        db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        snapshot=db.execute('SELECT pg_export_snapshot()').fetchone()[0]
        before=hashes(db);sums_before=money_totals(db)
        dump=tool('pg_dump.exe',source,['-Fc','--snapshot',snapshot,'--no-owner','--no-privileges'])
        assert dump.returncode==0,dump.stderr.decode(errors='replace')
    encrypted=cipher.encrypt(dump.stdout)
    artifact=OUT/(label+'.dump.fernet')
    artifact.write_bytes(encrypted)
    verified=cipher.decrypt(artifact.read_bytes());assert verified==dump.stdout
    dest='tradespace_audit_restore_'+uuid.uuid4().hex[:10];create(dest)
    restored=tool('pg_restore.exe',dest,['--single-transaction','--exit-on-error','--no-owner','--no-privileges'],verified)
    result={'source':source,'destination':dest,'encryption':'Fernet local drill; NOT production age/vault policy','plaintext_sha256':hashlib.sha256(verified).hexdigest(),'encrypted_sha256':hashlib.sha256(encrypted).hexdigest(),'decryption_equal':True,'restore_exit':restored.returncode,'source_tables':before,'ordinary_restore_no_disabled_triggers':True,'elapsed_seconds':round(time.monotonic()-start,2)}
    if restored.returncode:
        result['error']=restored.stderr.decode(errors='replace')[-5000:]
    else:
        with connect(dest) as db:after=hashes(db);sums_after=money_totals(db)
        result['restored_tables']=after;result['all_table_hashes_equal']=before==after
        assert before==after and sums_before==sums_after
        result['source_money_totals']=sums_before;result['restored_money_totals']=sums_after;result['money_totals_equal']=True
    report[label]=result
    (OUT/'restore-drill.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(label,'restore_exit',restored.returncode,'hashes_equal',result.get('all_table_hashes_equal'),'destination',dest,flush=True)

drill('tradespace_dev','clean-qa-populated')
