import hashlib
import hmac
import re
import unicodedata
from urllib.parse import parse_qsl, quote


HMAC_V2 = 'v2'
NONCE_RE = re.compile(r'^[A-Za-z0-9._~-]{16,128}$')
SIGNATURE_RE = re.compile(r'^[0-9a-fA-F]{64}$')


class MerchantHmacError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def normalize_path(path: str) -> str:
    normalized = unicodedata.normalize('NFC', path or '/')
    if not normalized.startswith('/'):
        normalized = '/' + normalized
    return quote(normalized, safe='/-._~')


def canonical_query(raw_query: bytes | str) -> str:
    if isinstance(raw_query, bytes):
        try:
            query_text = raw_query.decode('utf-8', errors='strict')
        except UnicodeDecodeError as exc:
            raise MerchantHmacError('bad_query_encoding') from exc
    else:
        query_text = raw_query
    try:
        items = parse_qsl(
            query_text,
            keep_blank_values=True,
            strict_parsing=False,
            encoding='utf-8',
            errors='strict',
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise MerchantHmacError('bad_query_encoding') from exc
    encoded = [
        (
            quote(key, safe='-._~', encoding='utf-8', errors='strict'),
            quote(value, safe='-._~', encoding='utf-8', errors='strict'),
        )
        for key, value in items
    ]
    encoded.sort(key=lambda item: (item[0].encode(), item[1].encode()))
    return '&'.join(f'{key}={value}' for key, value in encoded)


def normalize_content_type(value: str | None) -> str:
    return ' '.join((value or '').strip().lower().split())


def validate_nonce(value: str | None) -> str:
    nonce = (value or '').strip()
    if not NONCE_RE.fullmatch(nonce):
        raise MerchantHmacError('bad_nonce')
    return nonce


def canonical_request_v2(
    *,
    timestamp: str,
    nonce: str,
    method: str,
    path: str,
    query: bytes | str,
    content_type: str | None,
    body: bytes,
) -> str:
    return '\n'.join(
        (
            HMAC_V2,
            timestamp,
            validate_nonce(nonce),
            method.upper(),
            normalize_path(path),
            canonical_query(query),
            normalize_content_type(content_type),
            hashlib.sha256(body).hexdigest(),
        )
    )


def sign_request_v2(secret: str, canonical_request: str) -> str:
    return hmac.new(
        secret.encode('utf-8'),
        canonical_request.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()


def verify_request_v2(
    secret: str,
    canonical_request: str,
    signature: str,
) -> bool:
    candidate = (signature or '').strip()
    if not SIGNATURE_RE.fullmatch(candidate):
        return False
    expected = sign_request_v2(secret, canonical_request)
    return hmac.compare_digest(expected, candidate.lower())
