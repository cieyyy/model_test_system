"""安全模块：Key 加密、掩码、平台主密码。"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet

from .db import DATA_DIR

_PBKDF2_ITERATIONS = 200_000
_SECRET_FILE = DATA_DIR / "secret.key"
_fernet: Fernet | None = None


def _load_fernet() -> Fernet:
    """加载或生成平台级加密密钥（用于 API Key 静态加密）。"""
    global _fernet
    if _fernet is not None:
        return _fernet
    if _SECRET_FILE.exists():
        key = _SECRET_FILE.read_bytes().strip()
    else:
        key = Fernet.generate_key()
        _SECRET_FILE.write_bytes(key)
        os.chmod(_SECRET_FILE, 0o600)
    _fernet = Fernet(key)
    return _fernet


def encrypt_secret(plaintext: str) -> str:
    return _load_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str) -> str:
    return _load_fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")


def mask_key(key: str) -> str:
    """脱敏：保留前缀和末 4 位，如 sk-****abcd。"""
    key = key.strip()
    if len(key) <= 8:
        return "****"
    prefix = key[:3] if key[:3].isascii() and not key[:3].isspace() else ""
    tail = key[-4:]
    return f"{prefix}{'*' * 8}{tail}"


def hash_password(password: str, salt: str | None = None) -> str:
    """PBKDF2-SHA256，返回 'salt$hash'。"""
    if salt is None:
        salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ITERATIONS
    )
    return f"{salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    return secrets.compare_digest(hash_password(password, salt), stored)
