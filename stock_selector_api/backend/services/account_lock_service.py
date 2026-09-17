"""账户锁定服务（暴力破解防护）。

设计（用户确认）：
- 双维度独立计数/锁定：user:{username} 与 ip:{client_ip}
- 登录失败与验证码失败共用同一个计数器与锁定状态
- 阈值分开配置：验证码失败达 captcha_max_failures 即锁；登录失败达 login_max_failures 即锁
- 锁定期到自动解锁（懒清理：检查时发现过期即清除）
- lock_seconds=0 表示不锁定
"""

from datetime import datetime, timedelta, timezone

from config import settings
from core.exceptions import LockedError
from core.logger import log
from repositories import login_locks


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def lock_keys(username: str, client_ip: str) -> list[str]:
    keys = [f"user:{username}"]
    if client_ip:
        keys.append(f"ip:{client_ip}")
    return keys


def _remaining(record: dict | None, now: datetime) -> int:
    """返回距锁定的剩余秒数；未锁定/已过期返回 0。"""
    if not record or not record.get("locked_until"):
        return 0
    until = _as_utc(record["locked_until"])
    if until is None or until <= now:
        return 0
    return int((until - now).total_seconds())


def _is_locked(record: dict | None, now: datetime) -> bool:
    return _remaining(record, now) > 0


async def check_locked(username: str, client_ip: str) -> int:
    """登录前检查是否被锁。任一维度锁定即拒绝，返回剩余秒数。"""
    now = _now()
    for key in lock_keys(username, client_ip):
        rec = await login_locks.get_by_key(key)
        remain = _remaining(rec, now)
        if remain > 0:
            return remain
        if rec and rec.get("fail_count", 0) > 0 and not rec.get("locked_until"):
            # 阈值未满但有过失败记录，不锁定
            continue
        # 锁定期已过：懒清理该维度（自动解锁）
        if rec and rec.get("locked_until"):
            await login_locks.clear_key(key)
            log.info("账户锁定过期自动解锁: {}", key)
    return 0


async def record_failure(username: str, client_ip: str, kind: str) -> int:
    """记录一次失败（kind in {"captcha","login"}），共用计数。
    任一维度超过对应阈值即锁定并返回剩余秒数；未锁定返回 0。
    """
    threshold = (
        settings.captcha_max_failures if kind == "captcha" else settings.login_max_failures
    )
    if settings.lock_seconds <= 0:
        return 0

    now = _now()
    locked_remain = 0
    for key in lock_keys(username, client_ip):
        # 先原子计数（并发安全：upsert-increment，杜绝双双 INSERT 冲突→500/丢计数）
        await login_locks.increment_if_not_locked(key)
        rec = await login_locks.get_by_key(key)
        count = (rec or {}).get("fail_count", 0)
        if count >= threshold:
            until = now + timedelta(seconds=settings.lock_seconds)
            await login_locks.set_locked(key, until, count)
            locked_remain = locked_remain or settings.lock_seconds
            log.warning("{} 触发锁定: {} 超过 {} 次", kind, key, threshold)
    if locked_remain > 0:
        # 订阅策略「account_locked」事件（仅首次触发锁定时发；已在锁定中不会走到这里）
        try:
            from services import notify_service

            await notify_service.emit("account_locked", username=username, ip=client_ip or "-",
                                      reason="captcha" if kind == "captcha" else "login",
                                      max_failures=threshold, lock_minutes=round(locked_remain / 60, 1))
        except Exception:
            log.exception("锁定通知发送异常")
    return locked_remain


async def clear_failures(username: str, client_ip: str) -> None:
    """登录成功后清空两个维度的失败记录。"""
    for key in lock_keys(username, client_ip):
        await login_locks.clear_key(key)


async def ensure_not_locked(username: str, client_ip: str) -> None:
    """登录入口调用：若被锁抛 LockedError。"""
    remain = await check_locked(username, client_ip)
    if remain > 0:
        minutes = max(1, round(remain / 60, 1))
        raise LockedError(minutes=minutes, remaining=remain)
