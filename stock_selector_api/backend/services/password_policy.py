"""密码策略服务：强度校验、历史防重用、定期强制改密判定与统一改密写入。

- validate_password_strength：按 password_* 配置校验长度与复杂度
- is_reusing_history：新密码与最近 N 个历史密码重复则拒绝
- apply_password：改密统一入口（校验→防重用→更新哈希+改密时间→记历史→裁剪）
- password_status_of / check_password_not_expired：定期改密的判定与业务网关（过期抛 428）
"""

import re
from datetime import datetime, timedelta, timezone

from config import settings
from core import security
from core.exceptions import BizError, PasswordExpiredError
from core.logger import log
from repositories import users, password_history


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _age_days(changed_at) -> int:
    """最近一次改密距今的天数；无时间戳（老数据）视为 0。"""
    if not changed_at:
        return 0
    delta = _now() - _as_utc(changed_at)
    return max(0, delta.days)


def validate_password_strength(password: str) -> None:
    """按配置校验密码强度，不满足抛 BizError（覆盖建号/自助改密/管理员重置）。"""
    if len(password) < settings.password_min_length:
        raise BizError(f"密码长度不能少于 {settings.password_min_length} 位")
    if settings.password_require_upper and not re.search(r"[A-Z]", password):
        raise BizError("密码必须包含大写字母")
    if settings.password_require_lower and not re.search(r"[a-z]", password):
        raise BizError("密码必须包含小写字母")
    if settings.password_require_digit and not re.search(r"[0-9]", password):
        raise BizError("密码必须包含数字")
    if settings.password_require_special and not re.search(r"[^A-Za-z0-9]", password):
        raise BizError("密码必须包含特殊符号")


async def is_reusing_history(user_id: str, new_password: str) -> bool:
    if settings.password_history_count <= 0:
        return False
    hashes = await password_history.recent_hashes(user_id, settings.password_history_count)
    return any(security.verify_password(new_password, h) for h in hashes)


def password_status_of(user: dict) -> dict:
    """计算登录响应的密码状态（是否过期 / 剩余天数 / 是否临期警告）。
    与认证方式无关，jwt/session 两种模式都生效。
    """
    if settings.password_expire_days <= 0:
        return {
            "password_expired": False,
            "password_expire_in_days": None,
            "password_expire_warning": False,
        }
    age = _age_days(user.get("password_changed_at"))
    expired = age >= settings.password_expire_days
    remaining = max(0, settings.password_expire_days - age)
    warn = (not expired) and settings.password_expire_warning_days > 0 and remaining <= settings.password_expire_warning_days
    return {
        "password_expired": expired,
        "password_expire_in_days": remaining,
        "password_expire_warning": warn,
    }


def check_password_not_expired(user: dict) -> None:
    """业务网关：密码过期抛 428（豁免接口在 auth 路由，不受本网关约束）。"""
    if settings.password_expire_days <= 0 or not user:
        return
    if password_status_of(user)["password_expired"]:
        raise PasswordExpiredError()


async def apply_password(user: dict, new_password: str) -> None:
    """改密统一写入：强度→防重用→更新哈希+改密时间→记历史并裁剪→(session 模式)吊销旧会话。"""
    validate_password_strength(new_password)
    if user.get("password_hash") and security.verify_password(new_password, user.get("password_hash", "")):
        raise BizError("新密码不能与当前密码相同")
    if await is_reusing_history(user["id"], new_password):
        raise BizError(f"新密码不能与最近 {settings.password_history_count} 次使用的密码相同")
    new_hash = security.hash_password(new_password)
    now = _now()
    await users.update(user["id"], {"password_hash": new_hash, "password_changed_at": now})
    if settings.password_history_count > 0:
        await password_history.add(user["id"], new_hash)
        await password_history.trim(user["id"], settings.password_history_count)
    if settings.auth_mode == "session":
        from repositories import sessions

        await sessions.delete_by_user(user["id"])
        log.info("密码已修改并吊销旧会话: user_id={}", user["id"])
    else:
        log.info("密码已修改: user_id={}", user["id"])


async def note_initial_password(user_id: str, password_hash: str) -> None:
    """建号时记录改密时间与初始密码历史（定期改密/防重用从建号起生效）。"""
    await users.update(user_id, {"password_changed_at": _now()})
    if settings.password_history_count > 0:
        await password_history.add(user_id, password_hash)
        await password_history.trim(user_id, settings.password_history_count)
