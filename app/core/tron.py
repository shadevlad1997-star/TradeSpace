import hashlib
import hmac
import re


TRC20_ADDRESS_RE = re.compile(r'^T[1-9A-HJ-NP-Za-km-z]{33}$')
BASE58_ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
BASE58_INDICES = {
    character: index
    for index, character in enumerate(BASE58_ALPHABET)
}


class TronAddressError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _base58decode(value: str) -> bytes:
    number = 0
    for character in value:
        index = BASE58_INDICES.get(character)
        if index is None:
            raise TronAddressError('invalid_trc20_address')
        number = number * 58 + index
    byte_length = max(1, (number.bit_length() + 7) // 8)
    decoded = number.to_bytes(byte_length, byteorder='big')
    leading_zeros = len(value) - len(value.lstrip('1'))
    if leading_zeros:
        decoded = b'\x00' * leading_zeros + decoded
    return decoded


def validate_trc20_address(address: str) -> str:
    normalized = (address or '').strip()
    if not TRC20_ADDRESS_RE.fullmatch(normalized):
        raise TronAddressError('invalid_trc20_address')
    decoded = _base58decode(normalized)
    if len(decoded) != 25:
        raise TronAddressError('invalid_trc20_address')
    if decoded[0] != 0x41:
        raise TronAddressError('invalid_trc20_network')
    body, checksum = decoded[:-4], decoded[-4:]
    expected = hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4]
    if not hmac.compare_digest(expected, checksum):
        raise TronAddressError('invalid_trc20_address')
    return normalized
