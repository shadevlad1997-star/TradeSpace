import ipaddress
from functools import lru_cache

from starlette.requests import Request

from app.core.config import settings


@lru_cache(maxsize=1)
def _trusted_proxy_networks() -> tuple[ipaddress._BaseNetwork, ...]:
    networks: list[ipaddress._BaseNetwork] = []
    for item in settings.trusted_proxy_ips:
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def _valid_ip(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def _is_trusted_proxy(remote_ip: str | None) -> bool:
    normalized = _valid_ip(remote_ip)
    if not normalized:
        return False
    parsed = ipaddress.ip_address(normalized)
    return any(parsed in network for network in _trusted_proxy_networks())


def client_ip_from_scope(scope) -> str:
    client = scope.get('client') or ('', 0)
    remote_ip = client[0] if client else ''
    headers = {k.lower(): v for k, v in (scope.get('headers') or [])}

    if _is_trusted_proxy(remote_ip):
        x_real_ip = _valid_ip(headers.get(b'x-real-ip', b'').decode('utf-8', errors='ignore'))
        if x_real_ip:
            return x_real_ip
        forwarded = headers.get(b'x-forwarded-for', b'').decode('utf-8', errors='ignore')
        for item in forwarded.split(','):
            parsed = _valid_ip(item)
            if parsed:
                return parsed

    return _valid_ip(remote_ip) or str(remote_ip or 'unknown')


def client_ip(request: Request) -> str:
    return client_ip_from_scope(request.scope)