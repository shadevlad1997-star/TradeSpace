"""Produce an allowlisted clean-server source artifact, never a local DB image."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT=Path(__file__).resolve().parents[1]
ROOT_FILES=('pyproject.toml','requirements.lock','Dockerfile','.dockerignore','alembic.ini','README.md','SECURITY.md','.env.production.example','.env.staging.example','docker-compose.production.yml','docker-compose.monitoring.yml')
SCRIPTS=('__init__.py','bootstrap_superadmin.py','migrate.py','preflight_v2.py','smoke_v2.py',
         'verify_package_install.py','backup_encrypted.sh','verify_encrypted_backup_restore.sh')
SUFFIXES={'.py','.html','.css','.js','.svg','.png','.ico','.woff2','.mako','.yml'}


def release_files(root=ROOT):
    paths=[root/name for name in ROOT_FILES]+[root/'scripts'/name for name in SCRIPTS]
    for folder in ('app','alembic','branding','monitoring'):
        paths += [p for p in (root/folder).rglob('*') if p.is_file() and (p.suffix in SUFFIXES or (p.parent == root/'app/static' and p.name.endswith(('.LICENSE.txt','.NOTICE.txt'))))
                  and '__pycache__' not in p.parts and not any(part.startswith('.') for part in p.relative_to(root).parts)]
    paths += [root/'nginx'/'production.conf']
    paths += list((root/'docs').glob('*.md'))
    return sorted(set(p for p in paths if p.is_file()))


def build(destination: Path,root=ROOT):
    destination=destination.resolve()
    if destination.is_relative_to(root.resolve()):raise ValueError('Release output must be outside repository')
    destination.parent.mkdir(parents=True,exist_ok=True)
    manifest={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in release_files(root)}
    with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED) as archive:
        for relative in manifest:archive.write(root/relative,relative)
        archive.writestr('RELEASE_MANIFEST.json',json.dumps(manifest,indent=2,sort_keys=True))
    with zipfile.ZipFile(destination) as archive:
        assert archive.testzip() is None
        for relative,expected in manifest.items():assert hashlib.sha256(archive.read(relative)).hexdigest()==expected
    return {'files':len(manifest),'sha256':hashlib.sha256(destination.read_bytes()).hexdigest(),'artifact':str(destination)}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    print(json.dumps(build(parser.parse_args().output),indent=2))

if __name__=='__main__':main()
