"""加解密、查重索引与脱敏。

契约 3.1：身份证/学籍号等敏感字段持久层加密，另存归一化 HMAC 查重索引；
响应只给 masked 值；日志与导出不得出现完整证件与手机号。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import conf

_NONCE_BYTES = 12
_ID_PATTERN = re.compile(r"\d{17}[\dXx]")
_PHONE_PATTERN = re.compile(r"1\d{10}")


def _key() -> bytes:
    raw = (conf.CRYPTO_KEY or "").strip()
    if raw:
        try:
            decoded = base64.b64decode(raw, validate=True)
        except Exception:  # noqa: BLE001 - 配置错就退回派生密钥
            decoded = b""
        if len(decoded) in (16, 24, 32):
            return decoded
    return hashlib.sha256(conf.JWT_SECRET.encode("utf-8")).digest()


def encrypt_text(plain: Optional[str]) -> str:
    """AES-256-GCM，输出 base64(nonce + ciphertext)。空值返回空串。"""
    if not plain:
        return ""
    aes = AESGCM(_key())
    nonce = os.urandom(_NONCE_BYTES)
    blob = aes.encrypt(nonce, plain.encode("utf-8"), None)
    return base64.b64encode(nonce + blob).decode("ascii")


def decrypt_text(token: Optional[str]) -> str:
    if not token:
        return ""
    try:
        raw = base64.b64decode(token.encode("ascii"))
        nonce, blob = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
        return AESGCM(_key()).decrypt(nonce, blob, None).decode("utf-8")
    except Exception:  # noqa: BLE001 - 密钥轮换或数据损坏时视为无值
        return ""


def normalize_id_number(value: Optional[str]) -> str:
    return (value or "").strip().upper()


def normalize_phone(value: Optional[str]) -> str:
    return re.sub(r"\D", "", value or "")


def lookup_hash(value: Optional[str]) -> str:
    """归一化后的 HMAC-SHA256 查重索引，不可逆。"""
    if not value:
        return ""
    return hmac.new(
        _key(), value.strip().upper().encode("utf-8"), hashlib.sha256
    ).hexdigest()


def mask_id_number(value: Optional[str]) -> str:
    text = (value or "").strip()
    if len(text) < 8:
        return "****"
    return f"{text[:6]}{'*' * (len(text) - 10)}{text[-4:]}"


def mask_phone(value: Optional[str]) -> str:
    text = normalize_phone(value)
    if len(text) < 7:
        return "***"
    return f"{text[:3]}****{text[-4:]}"


def mask_name(value: Optional[str]) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) == 1:
        return text
    return text[0] + "*" * (len(text) - 1)


def mask_student_number(value: Optional[str]) -> str:
    text = (value or "").strip()
    if len(text) <= 4:
        return "****"
    return f"{text[:2]}{'*' * (len(text) - 4)}{text[-2:]}"


def mask_filename(name: Optional[str]) -> str:
    """文件名里出现的身份证号/手机号一律打码。"""
    text = (name or "").strip()
    text = _ID_PATTERN.sub(lambda m: mask_id_number(m.group()), text)
    text = _PHONE_PATTERN.sub(lambda m: mask_phone(m.group()), text)
    return text


def hash_token(token: str) -> str:
    """一次性凭证（注册邀请）只存哈希。"""
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
