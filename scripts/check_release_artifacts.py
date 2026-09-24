"""Reject private or generated artifacts from the public Git tree."""

from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath


PROHIBITED_SUFFIXES = {
    '.7z',
    '.backup',
    '.bak',
    '.db',
    '.dump',
    '.gz',
    '.key',
    '.p12',
    '.pem',
    '.pfx',
    '.pgdump',
    '.rar',
    '.sql',
    '.sqlite',
    '.sqlite3',
    '.tar',
    '.zip',
}
PROHIBITED_DIRECTORIES = {
    'backups',
    'private',
    'private-discovery',
}
IGNORED_SCAN_DIRECTORIES = {
    '.git',
    '.mypy_cache',
    '.pytest_cache',
    '.ruff_cache',
    '.venv',
    '__pycache__',
}


def tracked_files() -> list[str]:
    try:
        result = subprocess.run(
            ['git', 'ls-files', '-z'],
            check=True,
            capture_output=True,
        )
    except FileNotFoundError:
        root = Path.cwd()
        return [
            path.relative_to(root).as_posix()
            for path in root.rglob('*')
            if path.is_file()
            and not set(path.relative_to(root).parts).intersection(
                IGNORED_SCAN_DIRECTORIES
            )
        ]
    else:
        return [
            value.decode('utf-8', errors='strict')
            for value in result.stdout.split(b'\0')
            if value
        ]


def violation(path_value: str) -> str | None:
    path = PurePosixPath(path_value)
    lowered_name = path.name.lower()
    lowered_parts = {part.lower() for part in path.parts}

    if lowered_name == '.env':
        return 'environment file'
    if lowered_name.startswith('.env.') and not lowered_name.endswith('.example'):
        return 'environment file'
    if path.suffix.lower() in PROHIBITED_SUFFIXES:
        return f'prohibited artifact suffix {path.suffix.lower()}'
    if lowered_parts.intersection(PROHIBITED_DIRECTORIES):
        return 'private or backup directory'
    if 'private-discovery' in path_value.lower():
        return 'private discovery artifact'
    return None


def main() -> int:
    violations = [
        (path, reason)
        for path in tracked_files()
        if (reason := violation(path)) is not None
    ]
    if violations:
        for path, reason in violations:
            print(f'REJECTED: {path}: {reason}')
        return 1
    print('Release artifact policy: OK')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
