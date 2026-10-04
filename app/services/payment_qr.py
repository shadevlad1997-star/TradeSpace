"""Offline QR-1 validation: https://sbp.nspk.ru/api/new/ (C2B URL-based).
CRC syntax is checked; this is not a bank registration/authenticity attestation.
"""
from dataclasses import dataclass, replace
from decimal import Decimal
from io import BytesIO
import re
import secrets
from typing import Protocol
from urllib.parse import urlsplit, parse_qsl
import warnings
from PIL import Image, UnidentifiedImageError
import qrcode
import zxingcpp
from sqlalchemy import select, text

MAX_UPLOAD_BYTES = 1_000_000
MAX_SIDE = 4096
MAX_PIXELS = 8_000_000
QR_METHOD = 'sbp_qr'

class QrValidationError(ValueError):
    def __init__(self, message, code='qr_unsupported'):
        super().__init__(message)
        self.code = code

@dataclass(frozen=True)
class PaymentQr:
    payload: str
    identity: str
    source: str = 'uploaded_static'
    qr_type: str = 'static_open_amount'
    type_source: str = 'payload'

class BankQrProvider(Protocol):
    """QR-2 extension only. No provider is enabled in QR-1."""
    async def create_payment_qr(self, *, operation_key: str, amount: Decimal, currency: str) -> PaymentQr: ...


def inspect_payload(payload: str) -> PaymentQr:
    """Offline classification only; never fetch an uploaded URL or infer from QR ID."""
    if not isinstance(payload, str) or len(payload) > 512 or not payload.isascii() or any(c.isspace() for c in payload):
        raise QrValidationError('Некорректное содержимое QR.')
    url = urlsplit(payload)
    if url.scheme != 'https' or url.netloc != 'qr.nspk.ru' or url.fragment or not re.fullmatch(r'/[A-Z0-9]{32}', url.path):
        raise QrValidationError('QR прочитан, но его формат не поддерживается. Нужен платёжный QR NSPK.')
    if not url.query:
        return PaymentQr(payload, url.path[1:], qr_type='unknown', type_source='undetermined')
    try:
        pairs = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise QrValidationError('Некорректные параметры QR NSPK.') from exc
    params = dict(pairs)
    if len(params) != len(pairs) or set(params) - {'type', 'bank', 'cur', 'crc', 'sum'} or '%' in url.query:
        raise QrValidationError('Неподдерживаемые или повторяющиеся параметры QR.')
    if params.get('cur', 'RUB') != 'RUB' or not re.fullmatch(r'[0-9]{12}', params.get('bank', '')) or not re.fullmatch(r'[0-9A-Fa-f]{4}', params.get('crc', '')):
        raise QrValidationError('Некорректные банк, валюта или контрольное поле QR NSPK.')
    if 'type' not in params:
        if 'sum' in params:
            raise QrValidationError('Нужен статический QR со свободной суммой, без параметра sum.', 'qr_fixed_amount')
        return PaymentQr(payload, url.path[1:], qr_type='unknown', type_source='undetermined')
    if params['type'] == '02':
        return PaymentQr(payload, url.path[1:], source='bank_dynamic', qr_type='dynamic')
    if params['type'] != '01':
        raise QrValidationError('Этот тип QR NSPK не поддерживается для приёма оплаты.')
    return PaymentQr(payload, url.path[1:], qr_type='static_fixed_amount' if 'sum' in params else 'static_open_amount')


def validate_payload(payload: str, *, declared_static: bool = False) -> PaymentQr:
    qr = inspect_payload(payload)
    if qr.qr_type == 'unknown':
        if declared_static:
            # The configured static-upload mode is an explicit user declaration,
            # not a bank attestation. Never alter the bank URL.
            return replace(qr, qr_type='static_open_amount', type_source='trader_selection')
        raise QrValidationError('Укажите «Статический QR», чтобы использовать короткую ссылку NSPK.', 'qr_type_unknown')
    if qr.qr_type == 'dynamic':
        raise QrValidationError('Распознан динамический QR. В статическом режиме он не принимается.', 'qr_dynamic')
    if qr.qr_type != 'static_open_amount':
        raise QrValidationError('Нужен статический QR со свободной суммой, без параметра sum.', 'qr_fixed_amount')
    return qr


def decode_image(content: bytes, *, declared_static=False) -> PaymentQr:
    if not content or len(content) > MAX_UPLOAD_BYTES:
        raise QrValidationError('Загрузите PNG/JPEG размером не более 1 МБ.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                if image.format not in {'PNG','JPEG'} or getattr(image,'n_frames',1) != 1:
                    raise QrValidationError('Поддерживается одно изображение PNG или JPEG.')
                w,h=image.size
                if min(w,h)<64 or max(w,h)>MAX_SIDE or w*h>MAX_PIXELS:
                    raise QrValidationError('Размер изображения: от 64 до 4096 пикселей, не более 8 мегапикселей.')
                image.load()
                codes=zxingcpp.read_barcodes(image.convert('RGB'), formats=zxingcpp.BarcodeFormat.QRCode)
                if len(codes)!=1 or not codes[0].valid:
                    raise QrValidationError('На изображении должен быть ровно один читаемый QR-код.', 'qr_unreadable')
                return validate_payload(codes[0].text, declared_static=declared_static)
    except QrValidationError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise QrValidationError('Не удалось прочитать PNG/JPEG с QR-кодом.', 'qr_unreadable') from exc


def image_png(payload: str) -> bytes:
    output=BytesIO()
    qrcode.make(payload).save(output,format='PNG')
    return output.getvalue()


async def uploaded_qr(upload, *, existing=None, declared_static=False) -> PaymentQr:
    from starlette.concurrency import run_in_threadpool
    from app.core.security import reveal_text
    if upload is not None and getattr(upload,'filename',None):
        content=await upload.read(MAX_UPLOAD_BYTES+1)
        return await run_in_threadpool(decode_image, content, declared_static=declared_static)
    if existing is not None and existing.method==QR_METHOD and existing.qr_identity:
        payload = reveal_text(existing.value_encrypted)
        return validate_payload(payload, declared_static=declared_static)
    raise QrValidationError('Для QR NSPK загрузите PNG/JPEG со статическим QR и свободной суммой.')


def qr_audit_details(qr: PaymentQr | None) -> dict:
    if qr is None:
        return {}
    from hashlib import sha256
    return {'qr_mode': 'static', 'qr_type': qr.qr_type, 'qr_type_source': qr.type_source,
            'qr_payload_sha256': sha256(qr.payload.encode('utf-8')).hexdigest()}


async def lock_qr_routing(db):
    # Before Requisite/Trader row locks in BOTH APIs; only QR creates serialize.
    # The partial UNIQUE index remains the final database barrier.
    await db.execute(text('SELECT pg_advisory_xact_lock(812701, 1)'))


async def qr_is_available(db, identity: str, amount: Decimal) -> bool:
    from app.models import Deposit
    return not await db.scalar(select(Deposit.id).where(Deposit.qr_identity==identity,
        Deposit.qr_reserved.is_(True), Deposit.amount==amount, Deposit.currency=='RUB').limit(1))


def attach_snapshot(deposit, selected):
    if deposit.method != QR_METHOD:
        return
    qr=validate_payload(selected.value, declared_static=True)
    deposit.qr_identity=qr.identity
    deposit.qr_reserved=True
    deposit.qr_token=secrets.token_urlsafe(32)
    deposit.qr_snapshot={**selected.payment_details(), 'qr_type':qr.qr_type, 'qr_source':qr.source, 'qr_type_source':qr.type_source, 'payment_url':qr.payload}


def payment_details(deposit):
    if deposit.method != QR_METHOD or not deposit.qr_snapshot:
        return None
    return {**deposit.qr_snapshot, 'qr':{
        'type':'static_open_amount', 'source':'uploaded_static',
        'payment_url':deposit.qr_snapshot['payment_url'],
        'image_url':f'/pay/qr/{deposit.qr_token}/image',
        'checkout_url':f'/pay/qr/{deposit.qr_token}',
        'amount':str(deposit.amount), 'currency':deposit.currency,
        'expires_at':deposit.expires_at.isoformat(),
    }}
