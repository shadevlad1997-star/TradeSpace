"""Prepare source for an independent private repository; never copy history or data.

This writes a NEW directory only, without initializing Git, staging, committing,
pushing, starting services or reading local credentials. Tests and synthetic
fixture generators are source; generated QA records and attachments are not.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil
from scripts.check_release_artifacts import violation

ROOT = Path(__file__).resolve().parents[1]
GIT_ATTRIBUTES = b'# Preserve accepted Freeze hashes across operating systems.\n* -text whitespace=trailing-space,space-before-tab,cr-at-eol\n*.sh text eol=lf\n'
ROOT_FILES = (
    '.dockerignore', '.gitignore', '.gitleaks.toml', '.env.production.example',
    '.env.staging.example', 'alembic.ini', 'Dockerfile', 'pyproject.toml',
    'requirements.lock', 'README.md', 'SECURITY.md', 'docker-compose.production.yml',
    'docker-compose.monitoring.yml', 'docker-compose.test.yml',
)
FOLDERS = ('app','alembic','branding','docs','tests','scripts','.github','monitoring','nginx','rehearsal')
SUFFIXES = {'.py','.html','.css','.js','.svg','.png','.ico','.woff2','.mako','.yml','.yaml',
            '.cjs','.mjs','.toml','.json','.md','.txt','.conf','.sh','.ps1','.webmanifest'}
EXCLUDE_PARTS = {'__pycache__','.pytest_cache','.git','.venv','node_modules','certs','uploads',
                 'test-results','logs','tmp','run','backups','private'}


def source_files(root: Path):
    paths = [root / name for name in ROOT_FILES if (root / name).is_file()]
    for folder in FOLDERS:
        for path in (root / folder).rglob('*'):
            parts = path.relative_to(root).parts
            if set(parts) & EXCLUDE_PARTS or not path.is_file():
                continue
            if any(part.startswith('.') and part != '.github' for part in parts):
                continue
            if path.suffix.lower() not in SUFFIXES:
                continue
            paths.append(path)
    for path in sorted(set(paths)):
        relative = path.relative_to(root).as_posix()
        if violation(relative):
            raise ValueError(f'Unsafe source candidate: {relative}')
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f'External source link: {relative}')
        yield path


def prepare(destination: Path, root: Path = ROOT):
    destination = destination.resolve()
    if destination.is_relative_to(root.resolve()) or root.resolve().is_relative_to(destination):
        raise ValueError('Destination must be separate from the working repository')
    if destination.exists():
        raise ValueError('Destination must not exist; existing files are never overwritten')
    manifest_path = destination.with_name(destination.name + '.manifest.json')
    if manifest_path.exists():
        raise ValueError('Manifest already exists')
    files = list(source_files(root))
    manifest = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    destination.mkdir(parents=True)
    for relative, digest in manifest.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / relative, target)
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise RuntimeError(f'Copy verification failed: {relative}')
    # New-repository metadata only: do not change attributes/history of the source.
    (destination / '.gitattributes').write_bytes(GIT_ATTRIBUTES)
    manifest['.gitattributes'] = hashlib.sha256(GIT_ATTRIBUTES).hexdigest()
    # A sibling manifest avoids self-reference and is not application content.
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8', newline='\n')
    return {'directory': str(destination), 'files': len(manifest), 'manifest': str(manifest_path),
            'manifest_sha256': hashlib.sha256(manifest_path.read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    print(json.dumps(prepare(parser.parse_args().output), indent=2))

if __name__ == '__main__':
    main()
