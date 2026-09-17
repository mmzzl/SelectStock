"""安全基础设施：密码哈希（pwdlib）+ JWT/Session 双模式令牌。

- AUTH_MODE=jwt    : 签发 access/refresh 两份无状态 JWT
- AUTH_MODE=session: 签发随机会话 token（server 端存 DB），依赖方解析
本模块不直接依赖 DB，token 的"是谁"统一对外暴露为 claims dict。
"""

import secrets
from datetime import datetime, timedelta, timezone

import jwt
from pwdlib import PasswordHash

from config import settings

password_hash = PasswordHash.recommended()


def hash_password(plain: str) -> str:
    return password_hash.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return password_hash.verify(plain, hashed)
    except Exception:
        return False


# ---------------------------------------------------------------
# 通用 claims 构建
# ---------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sub_from_user(user_id: str) -> str:
    return f"uid:{user_id}"


def _user_id_from_sub(sub: str) -> str:
    return sub.split(":", 1)[1] if ":" in sub else sub


# ---------------------------------------------------------------
# JWT 模式
# ---------------------------------------------------------------

def create_access_token(user_id: str, extra: dict | None = None) -> str:
    payload = {
        "sub": _sub_from_user(user_id),
        "type": "access",
        "iat": _now(),
        "exp": _now() + timedelta(minutes=settings.jwt_access_expire_minutes),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_refresh_token(user_id: str) -> str:
    payload = {
        "sub": _sub_from_user(user_id),
        "type": "refresh",
        "iat": _now(),
        "exp": _now() + timedelta(days=settings.jwt_refresh_expire_days),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str, expected_type: str | None = None) -> dict:
    """校验并解码。非法/过期抛 jwt 异常，调用方捕获后转统一错误。"""
    payload = jwt.decode(
        token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
    )
    if expected_type and payload.get("type") != expected_type:
        raise jwt.InvalidTokenError(f"期望 {expected_type} 令牌，实际 {payload.get('type')}")
    return payload


# ---------------------------------------------------------------
# Session 模式
# ---------------------------------------------------------------

def create_session_token() -> str:
    """无状态随机 token，配合 server 端 sessions 表一起用。"""
    return secrets.token_urlsafe(48)


def session_cookie_max_age() -> int:
    return settings.session_expire_hours * 3600


# ---------------------------------------------------------------
# 通知渠道敏感字段落盘加密（AES-GCM）
# ---------------------------------------------------------------

def _secret_key() -> bytes:
    import hashlib

    raw = (settings.jwt_secret or "insecure-default-notify-key").encode()
    return hashlib.sha256(raw).digest()


def encrypt_secret(plaintext: str) -> str:
    """AES-GCM 对称加密：SMTP 密码、钉钉/飞书机器人 secret 等落盘前加密。"""
    if not plaintext:
        return ""
    import base64
    import os

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    ct = AESGCM(_secret_key()).encrypt(nonce, plaintext.encode(), None)
    return base64.urlsafe_b64encode(nonce + ct).decode()


def decrypt_secret(token: str) -> str:
    if not token:
        return ""
    import base64

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    raw = base64.urlsafe_b64decode(token.encode())
    return AESGCM(_secret_key()).decrypt(raw[:12], raw[12:], None).decode()
