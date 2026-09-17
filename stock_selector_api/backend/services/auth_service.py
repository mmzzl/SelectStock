"""认证服务：登录、刷新、登出、当前用户，兼容 JWT/Session 双模式。

对外只暴露行为，不暴露令牌机制；具体牌子由 config.auth_mode 决定。
"""

from datetime import datetime, timedelta, timezone

import jwt

from config import settings
from core import security
from core.exceptions import AuthError, BizError, NotFoundError
from repositories import users, roles, sessions, rbac
from core.logger import log


def _clean_user_payload(user: dict) -> dict:
    return {k: v for k, v in user.items() if k != "password_hash"}


async def authenticate(username: str, password: str) -> dict:
    user = await users.get_by_username(username)
    if not user:
        raise AuthError("用户名或密码错误")
    if not user.get("is_active", True):
        raise AuthError("账号已被禁用")
    if not security.verify_password(password, user.get("password_hash", "")):
        raise AuthError("用户名或密码错误")
    return user


async def issue_tokens(user: dict) -> dict:
    """签发令牌。jwt 模式返回 access+refresh；session 模式返回 session_id。"""
    uid = user["id"]
    if settings.auth_mode == "session":
        token = security.create_session_token()
        expires_at = datetime.now(timezone.utc) + timedelta(hours=settings.session_expire_hours)
        await sessions.create(
            {"user_id": uid, "token": token, "expires_at": expires_at}
        )
        return {"session_id": token, "token_type": "session"}
    # jwt
    access = security.create_access_token(uid)
    refresh = security.create_refresh_token(uid)
    return {"access_token": access, "token_type": "bearer", "refresh_token": refresh}


async def login(username: str, password: str) -> dict:
    user = await authenticate(username, password)
    tokens = await issue_tokens(user)
    from services import password_policy

    tokens.update(password_policy.password_status_of(user))
    log.info("用户登录成功: {}", username)
    return tokens


async def refresh_login(refresh_token: str) -> dict:
    if settings.auth_mode == "session":
        # session 模式无 refresh 语义，重新登录
        raise AuthError("session 模式不支持刷新令牌")
    try:
        payload = security.decode_token(refresh_token, expected_type="refresh")
    except jwt.PyJWTError:
        raise AuthError("刷新令牌无效或已过期")
    uid = security._user_id_from_sub(payload["sub"])
    user = await users.get(uid)
    if not user or not user.get("is_active", True):
        raise AuthError("用户不存在或已禁用")
    new_access = security.create_access_token(uid)
    new_refresh = security.create_refresh_token(uid)
    return {"access_token": new_access, "token_type": "bearer", "refresh_token": new_refresh}


async def logout(user_id: str) -> None:
    if settings.auth_mode == "session":
        await sessions.delete_by_user(user_id)
    # jwt 模式无状态，令牌交由客户端丢弃


async def change_own_password(user_id: str, old_password: str, new_password: str) -> None:
    """自助改密：须验证旧密码，走统一策略（强度+防重用+刷新改密时间）。"""
    from services import password_policy

    user = await users.get(user_id)
    if not user:
        raise NotFoundError("用户不存在")
    if not security.verify_password(old_password, user.get("password_hash", "")):
        raise BizError("旧密码不正确")
    await password_policy.apply_password(user, new_password)


async def update_own_profile(user_id: str, data: dict) -> None:
    """自助修改个人资料（显示名/邮箱）。"""
    await users.update(user_id, data)


async def current_user(user_id: str) -> dict:
    """把持有令牌的用户解析成完整的用户信息（含角色、权限）。"""
    user = await users.get(user_id)
    if not user:
        raise NotFoundError("用户不存在")
    if not user.get("is_active", True):
        raise AuthError("账号已被禁用")
    role_ids = await users.role_ids(user_id)
    role_rows = await roles._find_in("id", role_ids)
    role_codes = [r["code"] for r in role_rows]
    perms = await rbac.permission_codes_of_user_flat(user_id)
    return {
        **_clean_user_payload(user),
        "roles": role_codes,
        "permissions": perms,
    }
