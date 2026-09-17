"""认证接口：登录、刷新、登出、当前用户信息、动态菜单、验证码。"""

import secrets

from fastapi import APIRouter, Depends, Request, Response

from config import settings
from core import security
from core.dependencies import CurrentUser, CurrentUserDep
from core.response import ok
from schemas.auth import ChangePasswordIn, LoginIn, RefreshIn, UpdateProfileIn, UserInfo
from schemas.auth import MenuNode
from services import auth_service
from repositories import rbac

router = APIRouter(prefix="/auth", tags=["认证"])


def _anon_session(request: Request, response: Response) -> str:
    """读取（不存在则签发）绑定验证码的匿名会话 cookie。"""
    name = settings.captcha_cookie_name
    sid = request.cookies.get(name)
    if not sid:
        sid = secrets.token_urlsafe(32)
        response.set_cookie(
            key=name,
            value=sid,
            max_age=settings.captcha_expire_seconds,
            httponly=settings.captcha_cookie_httponly,
            secure=settings.session_cookie_secure if hasattr(settings, "session_cookie_secure") else False,
            samesite="lax",
            path="/",
        )
    return sid


@router.get("/captcha", summary="获取登录验证码")
async def get_captcha(request: Request, response: Response):
    from services import captcha_service

    sid = _anon_session(request, response)
    data = await captcha_service.issue_captcha(session_key=sid)
    return ok(data, message="获取验证码成功")


def _set_session_cookie(response: Response, tokens: dict) -> None:
    """session 模式把 session_id 写入 HttpOnly Cookie。"""
    if settings.auth_mode == "session" and tokens.get("session_id"):
        response.set_cookie(
            key=settings.session_cookie_name,
            value=tokens["session_id"],
            max_age=security.session_cookie_max_age(),
            httponly=settings.session_cookie_httponly,
            secure=settings.session_cookie_secure,
            samesite="lax",
            path="/",
        )


def _client_ip(request: Request) -> str:
    """取客户端 IP：信任代理头时取 X-Forwarded-For 首个 IP。"""
    if settings.lock_trust_proxy:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


@router.post("/login", summary="登录")
async def login(body: LoginIn, request: Request, response: Response):
    from core.exceptions import BizError, CaptchaError, LockedError
    from services import account_lock_service
    from services import auth_service as _auth_service

    client_ip = _client_ip(request)

    # 1. 前置检查：账号/IP 是否已被锁定
    await account_lock_service.ensure_not_locked(body.username, client_ip)

    # 2. 验证码校验（默认开启）
    if settings.captcha_enabled:
        from services import captcha_service

        sid = request.cookies.get(settings.captcha_cookie_name) or ""
        try:
            await captcha_service.verify_captcha(body.captcha_id, body.captcha_code, sid)
        except CaptchaError:
            # 验证码输入失败计入暴力计数（可能触发锁定）
            await account_lock_service.record_failure(body.username, client_ip, "captcha")
            # 订阅策略「login_failure」事件（策略默认关；可配发送间隔节流）
            from services import notify_service

            await notify_service.emit("login_failure", username=body.username, ip=client_ip or "-", reason="captcha")
            # 重新锁定检查：若这次触发锁定则返回 429
            remain = await account_lock_service.check_locked(body.username, client_ip)
            if remain > 0:
                minutes = max(1, round(remain / 60, 1))
                raise LockedError(minutes=minutes, remaining=remain)
            raise

    # 3. 账号密码校验（失败计入登录失败计数，可能触发锁定）
    try:
        tokens = await _auth_service.login(body.username, body.password)
    except BizError:
        await account_lock_service.record_failure(body.username, client_ip, "login")
        # 订阅策略「login_failure」事件
        from services import notify_service

        await notify_service.emit("login_failure", username=body.username, ip=client_ip or "-", reason="login")
        remain = await account_lock_service.check_locked(body.username, client_ip)
        if remain > 0:
            minutes = max(1, round(remain / 60, 1))
            raise LockedError(minutes=minutes, remaining=remain)
        raise

    # 4. 登录成功：清除该用户名/IP 的历史失败计数
    await account_lock_service.clear_failures(body.username, client_ip)
    _set_session_cookie(response, tokens)
    # 登录成功后清掉匿名会话 cookie，避免残留
    if settings.captcha_enabled:
        response.delete_cookie(key=settings.captcha_cookie_name, path="/")
    return ok(tokens, message="登录成功")


@router.post("/refresh", summary="刷新令牌")
async def refresh(body: RefreshIn):
    tokens = await auth_service.refresh_login(body.refresh_token)
    return ok(tokens, message="刷新成功")


@router.post("/logout", summary="登出")
async def logout(user: CurrentUserDep):
    await auth_service.logout(user.id)
    return ok(message="登出成功")


@router.post("/change-password", summary="自助修改密码")
async def change_password(body: ChangePasswordIn, user: CurrentUserDep):
    await auth_service.change_own_password(user.id, body.old_password, body.new_password)
    return ok(message="密码修改成功")


@router.put("/me", summary="修改个人资料")
async def update_profile(body: UpdateProfileIn, user: CurrentUserDep):
    await auth_service.update_own_profile(user.id, body.model_dump(exclude_unset=True))
    return ok(message="资料更新成功")


@router.get("/me", summary="当前用户信息")
async def me(user: CurrentUserDep):
    info = UserInfo(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        is_superuser=user.is_superuser,
        roles=user.roles or [],
        permissions=user.permissions or [],
    )
    return ok(info.model_dump())


@router.get("/menus", summary="当前用户可用的动态菜单树")
async def my_menus(user: CurrentUserDep):
    tree = await rbac.visible_menus_of_user(user.id)
    return ok(tree)
