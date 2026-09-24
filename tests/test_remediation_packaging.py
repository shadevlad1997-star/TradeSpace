"""Artifact and local-reset fail-closed boundaries, independent of production state."""
import json
from pathlib import Path
import zipfile
import pytest
from scripts.build_release import build
from scripts import qa_reset


def test_release_artifact_excludes_runtime_secrets_and_qa_even_when_present(tmp_path):
    root=tmp_path/'source';root.mkdir()
    safe=['app/main.py','app/templates/page.html','app/static/swagger-ui.LICENSE.txt','app/static/swagger-ui.NOTICE.txt','app/static/swagger-ui-bundle.js.LICENSE.txt','app/static/normalize.LICENSE.txt','app/static/tradespace/icon.svg','scripts/bootstrap_superadmin.py','requirements.lock','docs/CLEAN_SERVER_START.md']
    unsafe=['.env','run/access.json','uploads/evidence.png','logs/error.log','backup.dump','scripts/qa_seed.py','scripts/qa_reset.py','tests/test_example.py','app/__pycache__/secret.py','app/.env']
    for name in safe+unsafe:
        p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('safe' if name in safe else 'canary-do-not-package',encoding='utf8')
    output=tmp_path/'release.zip';build(output,root)
    with zipfile.ZipFile(output) as z:
        assert set(z.namelist())==set(safe)|{'RELEASE_MANIFEST.json'}
        assert not any(b'canary-do-not-package' in z.read(name) for name in z.namelist())
        assert set(json.loads(z.read('RELEASE_MANIFEST.json')))==set(safe)
    with pytest.raises(ValueError):build(root/'release.zip',root)


def test_reset_refuses_active_runtime_before_any_backup_or_db_mutation(monkeypatch,tmp_path):
    monkeypatch.setattr(qa_reset,'local_environment',lambda **kwargs:{})
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*args):pass
    monkeypatch.setattr(qa_reset.socket,'create_connection',lambda *args,**kwargs:Connection())
    def forbidden(*args,**kwargs):raise AssertionError('No database access before runtime shutdown')
    monkeypatch.setattr(qa_reset.psycopg,'connect',forbidden)
    destination=tmp_path/'backup'
    with pytest.raises(RuntimeError,match='Stop project'):qa_reset.reset(destination)
    assert not destination.exists()


def test_qa_synthetic_address_is_valid_without_runtime_environment_import():
    from scripts.qa_seed import synthetic_trc20_address
    from app.core.tron import validate_trc20_address
    assert validate_trc20_address(synthetic_trc20_address())



def test_private_source_copy_preserves_tests_and_0027_but_not_history_or_data(tmp_path):
    from scripts.prepare_repository import prepare
    root=tmp_path/'source';root.mkdir()
    safe=['app/main.py','alembic/versions/0027_aggregator_credentials.py','tests/golden/snapshots/gb-01.json','tests/tradespace/shell_interactions.test.cjs',
          'scripts/qa_seed.py','.github/workflows/release-gates.yml','nginx/production.conf','monitoring/prometheus.yml','.env.production.example']
    unsafe=['.git/config','.env','uploads/evidence.png','run/qa-access.json','logs/api.log','tests/__pycache__/secret.py',
            'app/.env','nginx/certs/privkey.pem','tests/results.zip','backup.zip']
    for name in safe+unsafe:
        p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('safe' if name in safe else 'canary-do-not-package',encoding='utf8')
    output=tmp_path/'private-source';result=prepare(output,root)
    assert set(json.loads(Path(result['manifest']).read_text()))==set(safe)|{'.gitattributes'}
    assert set(p.relative_to(output).as_posix() for p in output.rglob('*') if p.is_file())==set(safe)|{'.gitattributes'}
    assert all(b'canary-do-not-package' not in p.read_bytes() for p in output.rglob('*') if p.is_file())
    with pytest.raises(ValueError):prepare(output,root)
    with pytest.raises(ValueError):prepare(root/'nested',root)


def test_deployment_artifact_contains_infrastructure_and_0027_without_tls_or_qa(tmp_path):
    root=tmp_path/'source';root.mkdir()
    safe=['app/main.py','alembic/versions/0027_aggregator_credentials.py','docker-compose.production.yml',
          'docker-compose.monitoring.yml','nginx/production.conf','monitoring/prometheus.yml']
    unsafe=['nginx/certs/privkey.pem','uploads/file.png','scripts/qa_seed.py','monitoring/backup.zip','monitoring/.env']
    for name in safe+unsafe:
        p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('safe' if name in safe else 'canary-do-not-package',encoding='utf8')
    output=tmp_path/'release.zip';build(output,root)
    with zipfile.ZipFile(output) as z:
        assert set(z.namelist())==set(safe)|{'RELEASE_MANIFEST.json'}
        assert all(b'canary-do-not-package' not in z.read(name) for name in z.namelist())



def test_docker_final_exclusions_do_not_drop_credential_services_or_migrations():
    import fnmatch
    root=Path(__file__).resolve().parents[1]
    patterns=(root/'.dockerignore').read_text().splitlines()
    last_include=max(i for i,line in enumerate(patterns) if line.startswith('!'))
    exclusions=[x for x in patterns[last_include+1:] if x and not x.startswith('#') and not x.endswith('/')]
    for name in ('app/services/aggregator_credentials.py','app/services/merchant_api_keys.py',
                 'app/templates/merchant_credentials.html','alembic/versions/0027_aggregator_credentials.py'):
        assert not any(fnmatch.fnmatchcase(name,pattern) for pattern in exclusions),name
