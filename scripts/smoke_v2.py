"""Safe GET-only TradeSpace v2.0 smoke checks."""

import argparse
import asyncio
from dataclasses import dataclass

import httpx

from app.core.config import settings


@dataclass(frozen=True)
class SmokeCheck:
    path: str
    passed: bool
    detail: str


async def run_smoke(
    base_url: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    openapi_enabled: bool | None = None,
) -> list[SmokeCheck]:
    should_check_openapi = (
        settings.OPENAPI_ENABLED
        if openapi_enabled is None
        else bool(openapi_enabled)
    )
    paths = ['/health', '/ready', '/version', '/staff/login']
    if should_check_openapi:
        paths.append('/openapi.json')
    checks = []
    async with httpx.AsyncClient(
        base_url=base_url.rstrip('/'),
        timeout=10,
        follow_redirects=False,
        transport=transport,
    ) as client:
        for path in paths:
            try:
                # Deliberately the only HTTP method used by this script.
                response = await client.get(path)
            except Exception as exc:
                checks.append(
                    SmokeCheck(
                        path,
                        False,
                        f'failed ({type(exc).__name__.lower()})',
                    )
                )
                continue
            passed = response.status_code == 200
            if path == '/health' and passed:
                payload = response.json()
                passed = (
                    payload.get('status') == 'ok'
                    and payload.get('version') == settings.APP_VERSION
                )
            elif path == '/ready' and passed:
                payload = response.json()
                passed = payload.get('status') == 'ready'
            elif path == '/version' and passed:
                payload = response.json()
                passed = payload == {'version': settings.APP_VERSION}
            elif path == '/staff/login' and passed:
                passed = 'text/html' in response.headers.get(
                    'content-type', ''
                ).lower()
            checks.append(
                SmokeCheck(path, passed, f'HTTP {response.status_code}')
            )
    return checks


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='GET-only TradeSpace v2.0 smoke'
    )
    parser.add_argument(
        '--base-url',
        default='http://localhost:8000',
        help='Running API base URL',
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    checks = asyncio.run(run_smoke(args.base_url))
    for item in checks:
        print(
            f'{"PASS" if item.passed else "FAIL"} '
            f'{item.path}: {item.detail}'
        )
    passed = sum(1 for item in checks if item.passed)
    print(f'RESULT {passed}/{len(checks)} checks passed')
    return 0 if passed == len(checks) else 1


if __name__ == '__main__':
    raise SystemExit(main())
