import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
import jwt
from jwt import InvalidTokenError
from cryptography.fernet import Fernet
from cryptography.fernet import InvalidToken
from app.core.config import settings
pwd_hasher = PasswordHasher()
ALGORITHM = 'HS256'
SECRET_PREFIX = 'enc:v1:'
LEGACY_SECRET_PREFIX = 'enc:'

def hash_password(password: str) -> str:
    return pwd_hasher.hash(password)

def verify_password(password: str, hashed: str) -> bool:
    try:
        return pwd_hasher.verify(hashed, password)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False

def create_token(subject: str, token_type: str, expires_delta: timedelta, extra: dict[str, Any] | None = None) -> str:
    now = datetime.now(timezone.utc)
    payload = {'sub': subject, 'type': token_type, 'iat': now, 'exp': now + expires_delta, 'jti': secrets.token_urlsafe(16)}
    if extra: payload.update(extra)
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)

def decode_token(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
    except InvalidTokenError as exc:
        raise ValueError('invalid token') from exc

def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()

def auth_state_marker(password_hash: str) -> str:
    return sha256_text(password_hash)[:24]

def sign_hmac(secret: str, timestamp: str, body: bytes) -> str:
    return hmac.new(secret.encode(), timestamp.encode()+b'.'+body, hashlib.sha256).hexdigest()

def verify_hmac(secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    return hmac.compare_digest(sign_hmac(secret, timestamp, body), signature)

def _fernet() -> Fernet:
    try:
        return Fernet(settings.ENCRYPTION_KEY.encode())
    except Exception as exc:
        raise RuntimeError('ENCRYPTION_KEY must be a valid Fernet key. Generate it with Fernet.generate_key().') from exc

def encrypt_text(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()

def decrypt_text(value: str) -> str:
    return _fernet().decrypt(value.encode()).decode()


def _looks_like_fernet_token(value: str) -> bool:
    return value.startswith('gAAAA') and len(value) >= 80


def _encrypted_token(value: str) -> str | None:
    if value.startswith(SECRET_PREFIX):
        return value[len(SECRET_PREFIX):]
    if value.startswith(LEGACY_SECRET_PREFIX):
        return value[len(LEGACY_SECRET_PREFIX):]
    if _looks_like_fernet_token(value):
        return value
    return None


def is_encrypted_value(value: str | None) -> bool:
    if value is None:
        return False
    token = _encrypted_token(str(value).strip())
    if token is None:
        return False
    try:
        decrypt_text(token)
        return True
    except (InvalidToken, ValueError, TypeError):
        return False


def is_unreadable_encrypted_text(value: str | None) -> bool:
    if value is None:
        return False
    text = str(value).strip()
    token = _encrypted_token(text)
    if token is None:
        return False
    try:
        decrypt_text(token)
        return False
    except Exception:
        return True


def reveal_text(value: str | None) -> str:
    """Return a readable value while accepting legacy encrypted/plain rows."""
    if value is None:
        return ''
    text = str(value).strip()
    if not text:
        return ''
    token = _encrypted_token(text)
    if token is None:
        return text
    try:
        return decrypt_text(token)
    except Exception:
        return ''

def encrypt_secret(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value)
    token = _encrypted_token(text)
    if token is not None:
        try:
            decrypt_text(token)
        except Exception as exc:
            raise ValueError('Encrypted secret cannot be decrypted with the active ENCRYPTION_KEY') from exc
        return SECRET_PREFIX + token
    return SECRET_PREFIX + encrypt_text(text)

def decrypt_secret(value: str | None) -> str:
    if value is None:
        return ''
    text = str(value)
    token = _encrypted_token(text)
    if token is None:
        # Compatibility window for legacy rows. The migration canonicalizes them.
        return text
    try:
        return decrypt_text(token)
    except Exception:
        return ''


def mask_secret(value: str | None) -> str | None:
    return 'configured' if value else None

