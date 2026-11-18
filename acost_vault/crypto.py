"""Acost vault encryption format v1 (spec: docs/vault/FORMAT.md, vectors: docs/vault/vectors.json).

Only this module touches key material. `cryptography` is imported lazily so that
the rest of the client still runs on a Python without it (the old backend does).
Nothing here prints, logs or returns a key in an error message.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import uuid

FORMAT_VERSION = 1
KEY_BYTES = 32
NONCE_BYTES = 12
TAG_BYTES = 16
PAD_BLOCK = 256
RECOVERY_PREFIX = "ACOST1"
RECOVERY_CHECK_BYTES = 3
IDEM_INFO = b"acost idem v1"


class VaultCryptoError(Exception):
    """Decryption, integrity or format failure. Never carries key bytes."""


def _aesgcm(key):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise VaultCryptoError("加密版账本需要 cryptography 库；请用 ~/acost-venv/bin/python 运行。") from None
    if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_BYTES:
        raise VaultCryptoError("钥匙长度不对。")
    return AESGCM(bytes(key))


def canonical_uuid(value):
    """The lowercase hyphenated text form used in every AAD."""
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise VaultCryptoError("编号不是有效的 UUID。") from None


def new_key():
    return os.urandom(KEY_BYTES)


def canonical_json(value):
    """The one byte form of a document: sorted keys, no spaces, UTF-8, no NaN."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def pad(data):
    """ISO/IEC 7816-4: append 0x80, then zeros up to a multiple of 256 bytes."""
    data = bytes(data) + b"\x80"
    return data + b"\x00" * (-len(data) % PAD_BLOCK)


def unpad(data):
    if not data or len(data) % PAD_BLOCK:
        raise VaultCryptoError("补齐长度不对。")
    end = len(data.rstrip(b"\x00"))
    if end == 0 or data[end - 1] != 0x80:
        raise VaultCryptoError("补齐格式不对。")
    return data[:end - 1]


def _seal(key, plaintext, aad, key_version, nonce=None):
    nonce = os.urandom(NONCE_BYTES) if nonce is None else bytes(nonce)
    if len(nonce) != NONCE_BYTES:
        raise VaultCryptoError("nonce 长度不对。")
    if not isinstance(key_version, int) or not 1 <= key_version <= 0xFFFFFFFF:
        raise VaultCryptoError("钥匙版本不对。")
    body = _aesgcm(key).encrypt(nonce, plaintext, aad)
    return bytes([FORMAT_VERSION]) + key_version.to_bytes(4, "big") + nonce + body


def _open(key, blob, aad_for_version):
    blob = bytes(blob)
    if len(blob) < 1 + 4 + NONCE_BYTES + TAG_BYTES or blob[0] != FORMAT_VERSION:
        raise VaultCryptoError("密文格式版本不认识。")
    key_version = int.from_bytes(blob[1:5], "big")
    nonce, body = blob[5:5 + NONCE_BYTES], blob[5 + NONCE_BYTES:]
    try:
        from cryptography.exceptions import InvalidTag
    except ImportError:
        raise VaultCryptoError("加密版账本需要 cryptography 库；请用 ~/acost-venv/bin/python 运行。") from None
    try:
        return key_version, _aesgcm(key).decrypt(nonce, body, aad_for_version(key_version))
    except InvalidTag:
        raise VaultCryptoError("解密失败：密文被改过，或钥匙不对。") from None


def blob_key_version(blob):
    blob = bytes(blob)
    if len(blob) < 5 or blob[0] != FORMAT_VERSION:
        raise VaultCryptoError("密文格式版本不认识。")
    return int.from_bytes(blob[1:5], "big")


# --- the data key, wrapped by the master key -------------------------------------------

def wrap_aad(ledger_id, key_version):
    return ("acost-wrap-v1|%s|%d" % (canonical_uuid(ledger_id), key_version)).encode()


def wrap_key(master_key, data_key, ledger_id, key_version, nonce=None):
    if len(data_key) != KEY_BYTES:
        raise VaultCryptoError("数据钥匙长度不对。")
    return _seal(master_key, bytes(data_key), wrap_aad(ledger_id, key_version), key_version, nonce)


def unwrap_key(master_key, blob, ledger_id):
    key_version, data_key = _open(master_key, blob, lambda version: wrap_aad(ledger_id, version))
    if len(data_key) != KEY_BYTES:
        raise VaultCryptoError("数据钥匙长度不对。")
    return key_version, data_key


# --- blind fingerprints of idempotency keys -------------------------------------------

def idem_key(data_key):
    """HKDF-SHA256 (RFC 5869) with an all-zero salt, info "acost idem v1", 32 bytes."""
    prk = hmac.new(b"\x00" * 32, bytes(data_key), hashlib.sha256).digest()
    return hmac.new(prk, IDEM_INFO + b"\x01", hashlib.sha256).digest()


def idem_tag(fingerprint_key, idempotency_key):
    return hmac.new(fingerprint_key, idempotency_key.encode("utf-8"), hashlib.sha256).digest()


# --- records and snapshots ---------------------------------------------------------------

def record_aad(ledger_id, record_id, version, key_version):
    return ("acost-record-v1|%s|%s|%d|%d" % (canonical_uuid(ledger_id), canonical_uuid(record_id),
                                             version, key_version)).encode()


def snapshot_aad(ledger_id, upto_seq, key_version):
    return ("acost-snapshot-v1|%s|%d|%d" % (canonical_uuid(ledger_id), upto_seq, key_version)).encode()


def encrypt_record(data_key, key_version, ledger_id, record_id, version, document, nonce=None):
    return _seal(data_key, pad(canonical_json(document)),
                 record_aad(ledger_id, record_id, version, key_version), key_version, nonce)


def decrypt_record(data_key, ledger_id, record_id, version, blob):
    _, padded = _open(data_key, blob, lambda kv: record_aad(ledger_id, record_id, version, kv))
    return _loads(unpad(padded))


def encrypt_snapshot(data_key, key_version, ledger_id, upto_seq, document, nonce=None):
    return _seal(data_key, pad(canonical_json(document)), snapshot_aad(ledger_id, upto_seq, key_version),
                 key_version, nonce)


def decrypt_snapshot(data_key, ledger_id, upto_seq, blob):
    _, padded = _open(data_key, blob, lambda kv: snapshot_aad(ledger_id, upto_seq, kv))
    return _loads(unpad(padded))


def _loads(raw):
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise VaultCryptoError("解密后的内容不是 JSON。") from None
    if not isinstance(value, dict):
        raise VaultCryptoError("解密后的内容不是 JSON 对象。")
    return value


# --- recovery code -------------------------------------------------------------------------

def _recovery_check(master_key):
    return hashlib.sha256(b"acost recovery v1\x00" + bytes(master_key)).digest()[:RECOVERY_CHECK_BYTES]


def recovery_code(master_key):
    """ACOST1- then 56 base32 characters (key + 3 check bytes) in groups of four."""
    if len(master_key) != KEY_BYTES:
        raise VaultCryptoError("master key 长度不对。")
    text = base64.b32encode(bytes(master_key) + _recovery_check(master_key)).decode("ascii")
    return RECOVERY_PREFIX + "-" + "-".join(text[i:i + 4] for i in range(0, len(text), 4))


def parse_recovery_code(code):
    """Spaces, dashes and case do not matter; 0 and 1 are read as O and I."""
    if not isinstance(code, str):
        raise VaultCryptoError("恢复码格式不对。")
    text = re.sub(r"[\s\-]", "", code).upper().replace("0", "O").replace("1", "I")
    prefix = RECOVERY_PREFIX.replace("1", "I")
    if not text.startswith(prefix):
        raise VaultCryptoError("恢复码应以 ACOST1 开头。")
    text = text[len(prefix):]
    if len(text) != 56 or not re.fullmatch(r"[A-Z2-7]+", text):
        raise VaultCryptoError("恢复码长度或字符不对。")
    raw = base64.b32decode(text)
    master_key, check = raw[:KEY_BYTES], raw[KEY_BYTES:]
    if not hmac.compare_digest(check, _recovery_check(master_key)):
        raise VaultCryptoError("恢复码校验位不对，可能抄错了。")
    return master_key


def b64(data):
    return base64.b64encode(bytes(data)).decode("ascii")


def unb64(text):
    try:
        return base64.b64decode(text, validate=True)
    except (ValueError, TypeError):
        raise VaultCryptoError("密文不是有效的 Base64。") from None
