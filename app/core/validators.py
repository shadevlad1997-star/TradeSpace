import ipaddress
import socket
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from app.core.config import settings


BLOCKED_HOSTS = {'localhost', 'metadata.google.internal'}
BLOCKED_SUFFIXES = ('.local', '.internal', '.localhost')
DEV_WEBHOOK_HOSTS = {'localhost', '127.0.0.1', '::1', 'host.docker.internal'}


def normalize_amount(value: Decimal) -> Decimal:
    try:
        return Decimal(value).quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError('invalid amount') from exc


def _is_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    return any((
        ip.is_private,
        ip.is_loopback,
        ip.is_link_local,
        ip.is_reserved,
        ip.is_multicast,
        ip.is_unspecified,
    ))


def _resolve_host_ips(host: str, port: int) -> set[ipaddress._BaseAddress]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError('webhook_url host could not be resolved') from exc
    addresses: set[ipaddress._BaseAddress] = set()
    for info in infos:
        try:
            addresses.add(ipaddress.ip_address(info[4][0]))
        except (ValueError, IndexError):
            continue
    if not addresses:
        raise ValueError('webhook_url host could not be resolved')
    return addresses


def validate_public_webhook_url(value: str | None) -> str | None:
    if not value:
        return value
    parsed = urlparse(value)
    if parsed.scheme not in {'https', 'http'}:
        raise ValueError('webhook_url must use http or https')
    if not parsed.hostname:
        raise ValueError('webhook_url must contain hostname')
    try:
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    except ValueError as exc:
        raise ValueError('webhook_url port is invalid') from exc
    host = parsed.hostname.strip().lower().rstrip('.')
    if host == 'metadata.google.internal':
        raise ValueError('webhook_url host is not allowed')
    if not settings.is_production:
        if host in DEV_WEBHOOK_HOSTS:
            return value
        if host in BLOCKED_HOSTS or host.endswith(BLOCKED_SUFFIXES):
            raise ValueError('webhook_url host is not allowed')
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return value
        if _is_blocked_ip(ip):
            raise ValueError('webhook_url must not point to private or reserved IP ranges')
        return value
    if parsed.scheme != 'https':
        raise ValueError('production webhook_url must use https')
    if host in BLOCKED_HOSTS or host.endswith(BLOCKED_SUFFIXES):
        raise ValueError('webhook_url host is not allowed')
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        for resolved_ip in _resolve_host_ips(host, port):
            if _is_blocked_ip(resolved_ip):
                raise ValueError('webhook_url must not resolve to private or reserved IP ranges')
    else:
        if _is_blocked_ip(ip):
            raise ValueError('webhook_url must not point to private or reserved IP ranges')
    return value


def validate_ip_whitelist(values: list[str]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        item = value.strip()
        if not item:
            continue
        try:
            if '/' in item:
                normalized.append(str(ipaddress.ip_network(item, strict=False)))
            else:
                normalized.append(str(ipaddress.ip_address(item)))
        except ValueError as exc:
            raise ValueError(f'invalid IP address or network in whitelist: {item}') from exc
    return normalized


def ip_in_whitelist(ip: str, whitelist: list[str] | None) -> bool:
    if not whitelist:
        return True
    try:
        candidate = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for item in whitelist:
        try:
            if '/' in str(item):
                if candidate in ipaddress.ip_network(str(item), strict=False):
                    return True
            elif candidate == ipaddress.ip_address(str(item)):
                return True
        except ValueError:
            continue
    return False


def mask_destination(value: str) -> str:
    clean = ''.join(ch for ch in value if ch.isalnum() or ch in '+@')
    if len(clean) <= 4:
        return '****'
    return '*' * max(len(clean) - 4, 4) + clean[-4:]
