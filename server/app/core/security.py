"""安全：凭证加密、JWT、密码哈希"""
import os
import time
import base64
import hashlib
from typing import Any, Dict, Optional

import jwt
from cryptography.fernet import Fernet
from passlib.context import CryptContext

# 用于加密凭证的密钥：优先读环境变量，否则用固定开发密钥
SECRET_KEY = os.getenv("DF_SECRET_KEY", "df-market-dev-secret-key-change-me")
JWT_SECRET = os.getenv("DF_JWT_SECRET", "df-market-jwt-secret-change-me")
JWT_EXPIRE_HOURS = int(os.getenv("DF_JWT_EXPIRE_HOURS", "24"))


def _fernet() -> Fernet:
    key = hashlib.sha256(SECRET_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_token(plain: str) -> str:
    """加密 access_token 等敏感字段"""
    if not plain:
        return ""
    return _fernet().encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt_token(cipher: str) -> str:
    """解密，失败返回空串"""
    if not cipher:
        return ""
    try:
        return _fernet().decrypt(cipher.encode("utf-8")).decode("utf-8")
    except Exception:
        return ""


pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return pwd_context.verify(password, hashed)
    except Exception:
        return False


def create_access_token(data: Dict[str, Any], expire_hours: int = None) -> str:
    payload = dict(data)
    payload["exp"] = int(time.time()) + (expire_hours or JWT_EXPIRE_HOURS) * 3600
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def decode_access_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except Exception:
        return None


def mask_token(token: str) -> str:
    """打码显示，避免凭证泄露"""
    if not token:
        return ""
    if len(token) <= 8:
        return "*" * len(token)
    return f"{token[:4]}{'*' * 8}{token[-4:]}"
