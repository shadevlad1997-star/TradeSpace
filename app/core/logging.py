import json
import logging
import re
import sys
from datetime import datetime, timezone
from collections.abc import Mapping

from app.core.config import settings


RESERVED_LOG_RECORD_FIELDS = {
    'args',
    'asctime',
    'created',
    'exc_info',
    'exc_text',
    'filename',
    'funcName',
    'levelname',
    'levelno',
    'lineno',
    'module',
    'msecs',
    'message',
    'msg',
    'name',
    'pathname',
    'process',
    'processName',
    'relativeCreated',
    'stack_info',
    'thread',
    'threadName',
}

SENSITIVE_KEY_PARTS = {
    'authorization',
    'password',
    'secret',
    'token',
    'refresh',
    'access',
    'hmac',
    'signature',
    '2fa',
    'otp',
    'totp',
    'private_key',
    'seed',
    'api_key',
    'x-api-key',
    'x-signature',
}

BEARER_RE = re.compile(r'\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+', re.IGNORECASE)
URI_CREDENTIAL_RE = re.compile(r'(?P<prefix>[a-z][a-z0-9+.-]*://[^\s/@:]+:)[^\s/@]+(?=@)', re.IGNORECASE)
ASSIGNMENT_RE = re.compile(
    r"(?P<label>[\"']?\b(?:[\w-]*(?:password|secret|token|api[_-]?key|signature|private[_-]?key)|otp|totp)[\"']?\s*[:=]\s*)"
    r"(?:\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|[^&\s,;\]}]+)",
    re.IGNORECASE,
)


def _is_sensitive_key(key: object) -> bool:
    normalized = str(key or '').lower().replace('-', '_')
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


def mask_secret(value: object) -> str:
    text = str(value or '')
    if not text:
        return ''
    if len(text) <= 8:
        return '***redacted***'
    return f'{text[:4]}...{text[-4:]}'


def mask_token(value: object) -> str:
    text = str(value or '')
    if not text:
        return ''
    if len(text) <= 12:
        return '***redacted***'
    return f'{text[:6]}...{text[-6:]}'


def redact_sensitive(value, key: object | None = None):
    if _is_sensitive_key(key):
        return '***redacted***'
    if isinstance(value, Mapping):
        return {item_key: redact_sensitive(item_value, item_key) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive(item) for item in value)
    if isinstance(value, str):
        redacted = BEARER_RE.sub(lambda match: f'{match.group(1)} ***redacted***', value)
        redacted = URI_CREDENTIAL_RE.sub(lambda match: match.group('prefix') + '***redacted***', redacted)
        return ASSIGNMENT_RE.sub(lambda match: f'{match.group("label")}***redacted***', redacted)
    return value


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'level': record.levelname,
            'logger': record.name,
            'message': redact_sensitive(record.getMessage()),
            'service': settings.APP_NAME,
            'env': settings.ENV,
        }
        if record.exc_info:
            payload['exception'] = redact_sensitive(self.formatException(record.exc_info))
        elif record.exc_text:
            payload['exception'] = redact_sensitive(record.exc_text)
        if record.stack_info:
            payload['stack'] = redact_sensitive(record.stack_info)

        for key, value in record.__dict__.items():
            if key in RESERVED_LOG_RECORD_FIELDS or key.startswith('_'):
                continue
            safe_value = redact_sensitive(value, key)
            if isinstance(safe_value, (str, int, float, bool)) or safe_value is None:
                payload[key] = safe_value
            else:
                payload[key] = redact_sensitive(str(safe_value))
        return scrub_configured_secrets(json.dumps(payload, ensure_ascii=False, separators=(',', ':')))


def scrub_configured_secrets(text: str) -> str:
    # Also cover a configured credential printed without its field name.
    for key, value in settings.model_dump().items():
        if _is_sensitive_key(key) and isinstance(value, str) and len(value) >= 8:
            text = text.replace(value, '***redacted***')
    return text


class RedactingFormatter(logging.Formatter):
    """Sanitize the FINAL handler output, including cached exception text."""
    def __init__(self, inner: logging.Formatter):
        super().__init__()
        self.inner = inner

    def format(self, record: logging.LogRecord) -> str:
        return scrub_configured_secrets(redact_sensitive(self.inner.format(record)))


def protect_log_handlers() -> None:
    loggers = [logging.getLogger(), *(item for item in logging.Logger.manager.loggerDict.values()
                                    if isinstance(item, logging.Logger))]
    for logger in loggers:
        for handler in logger.handlers:
            if not isinstance(handler.formatter, (RedactingFormatter, JsonLogFormatter)):
                handler.setFormatter(RedactingFormatter(handler.formatter or logging.Formatter()))


def configure_logging() -> None:
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    formatter = (JsonLogFormatter() if settings.LOG_FORMAT.lower() == 'json'
                 else RedactingFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s')))
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(handler)
    protect_log_handlers()
    logging.getLogger('uvicorn.access').setLevel(logging.WARNING)
