"""依赖注入：当前用户解析 + 极简权限校验。业务层一行调用：

    @router.delete("/{id}", dependencies=[Depends(require_permission("device:delete"))])
"""

from dataclasses import dataclass
from typing import Annotated

import jwt
from fastapi import Depends, Request

from fastapi.security.utils import get_authorization_scheme_param

from config import settings
from core import security
from core.exceptions import AuthError, PermissionDeniedError
from services import auth_service


@dataclass
class CurrentUser:
    id: str
    username: str
    display_name: str = ""
    is_superuser: bool = False
    roles: list[str] = None
    permissions: list[str] = None
    raw: dict = None


def _bearer_token(request: Request) -> str:
    """从 Authorization: Bearer 或 session cookie 提取令牌。"""
    if settings.auth_mode == "session":
        return request.cookies.get(settings.session_cookie_name) or ""
    auth = request.headers.get("Authorization", "")
    scheme, param = get_authorization_scheme_param(auth)
    if not auth or scheme.lower() != "bearer":
        raise AuthError("未提供有效的认证令牌")
    return param


async def get_current_user(request: Request) -> CurrentUser:
    """把令牌解析为当前用户。JWT 验签、session 查库。"""
    token = _bearer_token(request)
    if not token:
        raise AuthError("未登录")

    if settings.auth_mode == "session":
        from datetime import datetime, timezone

        from repositories import sessions

        s = await sessions.get_by_token(token)
        if not s:
            raise AuthError("会话不存在或已过期")
        if s.get("expires_at") is not None:
            exp = s["expires_at"]
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp < datetime.now(timezone.utc):
                await sessions.delete(s["id"])
                raise AuthError("会话已过期")
        user_id = s["user_id"]
    else:
        try:
            payload = security.decode_token(token, expected_type="access")
            user_id = security._user_id_from_sub(payload["sub"])
        except jwt.PyJWTError:
            raise AuthError("令牌无效或已过期")

    info = await auth_service.current_user(user_id)
    return CurrentUser(
        id=info["id"],
        username=info.get("username", ""),
        display_name=info.get("display_name", ""),
        is_superuser=info.get("is_superuser", False),
        roles=info.get("roles", []),
        permissions=info.get("permissions", []),
        raw=info,
    )


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]


async def ensure_password_current(user: CurrentUserDep) -> None:
    """密码到期网关：过期时业务接口统一拒绝（HTTP 428）。
    用作 router 级 dependencies 挂到业务路由；登录/改密/登出/me/menus/refresh 等豁免接口在 auth 路由，不受约束。
    """
    from services import password_policy

    password_policy.check_password_not_expired(user.raw)


def require_permission(permission_code: str):
    """权限依赖：`dependencies=[Depends(require_permission("module:action"))]`。
    超管(" * ")直接放行；否则必须持有对应权限码。
    注意：该依赖自行完成登录校验，内层不要再注入 CurrentUserDep。
    """
    async def checker(request: Request) -> None:
        user = await get_current_user(request)
        if user.is_superuser or "*" in (user.permissions or []):
            return
        if permission_code not in (user.permissions or []):
            raise PermissionDeniedError(f"缺少权限: {permission_code}")
    return checker


def require_roles(*role_codes: str):
    """角色依赖：需拥有所列角色之一。
    用法：dependencies=[Depends(require_roles("superadmin", "admin"))]
    """
    async def checker(request: Request) -> None:
        user = await get_current_user(request)
        if user.is_superuser:
            return
        if not (set(role_codes) & set(user.roles or [])):
            raise PermissionDeniedError(f"需要角色: {'/'.join(role_codes)}")
    return checker
