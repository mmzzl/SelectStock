"""验证码服务：Pillow 生成图片验证码、会话绑定、校验（用后即焚）。

安全要点（参考产品 vericode 语义 + 防伪造强化）：
- 每次 GET 生成新码 + 签发匿名会话 cookie(anon_sid)，码与 cookie 绑定入库
- 登录校验三方一致：code 匹配 + 会话 cookie 匹配 + 未过期 + 未使用
- 校验后立即标记 used（用后即焚），同一 captcha_id 不可复用
- 字符集去除易混淆字符（0/O/1/I/l），校验大小写不敏感
"""

import base64
import random
import secrets
from datetime import datetime, timedelta, timezone
from io import BytesIO

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from config import settings
from core.logger import log
from repositories import captchas

# 去除易混淆字符：0/O/1/I/l/o
_CHARSET = "23456789abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ"


def _random_code(length: int) -> str:
    return "".join(random.SystemRandom().choice(_CHARSET) for _ in range(length))


def _draw_captcha(code: str, width: int = 160, height: int = 60) -> bytes:
    """用 Pillow 画一张带噪点、干扰线的验证码 PNG。"""
    img = Image.new("RGB", (width, height), _random_color(230, 255))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(38)  # Pillow>=10.1 支持无字体文件渲染
    except TypeError:  # 老版本 Pillow 兼容
        font = ImageFont.load_default()
        font = font.font_variant(size=38) if hasattr(font, "font_variant") else font

    # 干扰线
    for _ in range(random.randint(3, 5)):
        draw.line(
            [(random.randint(0, width), random.randint(0, height)),
             (random.randint(0, width), random.randint(0, height))],
            fill=_random_color(100, 200),
            width=random.randint(1, 2),
        )
    # 噪点
    for _ in range(random.randint(60, 100)):
        draw.point(
            (random.randint(0, width), random.randint(0, height)),
            fill=_random_color(120, 220),
        )
    # 字符（交错高度，轻微旋转）
    x = 12
    for ch in code:
        y = random.randint(2, 14)
        draw.text((x, y), ch, font=font, fill=_random_color(10, 120))
        x += random.randint(width // (len(code) + 2), width // len(code))
    img = img.filter(ImageFilter.SMOOTH)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _random_color(low: int, high: int) -> tuple[int, int, int]:
    return (
        random.randint(low, high),
        random.randint(low, high),
        random.randint(low, high),
    )


async def _cleanup_expired() -> None:
    """删除过期验证码（避免表持续膨胀）。"""
    rows = await captchas._find_all()
    now = datetime.now(timezone.utc)
    expired = [
        r["id"]
        for r in rows
        if r.get("expires_at") is not None and _as_utc(r["expires_at"]) < now
    ]
    for cid in expired:
        await captchas.delete(cid)


def _as_utc(dt) -> datetime:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


async def issue_captcha(session_key: str = "") -> dict:
    """生成验证码：入库（绑定匿名会话防伪造）+ 返回 id 与 base64 图片。"""
    await _cleanup_expired()
    code = _random_code(settings.captcha_length)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings.captcha_expire_seconds)
    record = await captchas.create(
        {"code": code, "expires_at": expires_at, "used": False, "session_key": session_key}
    )
    return {
        "captcha_id": record["id"],
        "image_base64": base64.b64encode(_draw_captcha(code)).decode(),
    }


async def verify_captcha(captcha_id: str, code: str, session_key: str) -> None:
    """校验验证码。任一条件不满足抛异常；成功后原子烧码用后即焚。

    - 状态类错误（已使用/过期/会话不匹配）：BizError，不计入暴力计数
    - 输入类错误（缺失/填写错误）：CaptchaError，计入锁定计数
    """
    from core.exceptions import BizError, CaptchaError

    if not settings.captcha_enabled:
        return
    if not captcha_id or not code:
        raise CaptchaError("请填写验证码")

    record = await captchas.get_by_id(captcha_id)
    if not record:
        raise BizError("验证码不存在或已失效")

    now = datetime.now(timezone.utc)
    expires = _as_utc(record.get("expires_at"))
    if record.get("used"):
        raise BizError("验证码已使用，请刷新")
    if expires is None or expires < now:
        await captchas.delete(captcha_id)
        raise BizError("验证码已过期，请刷新")

    # 防伪造核心：验证码与会话 cookie 绑定，跨客户端无法复用（错误/伪造即烧码）
    if record.get("session_key") and session_key != record.get("session_key"):
        await captchas.mark_used(captcha_id)
        raise BizError("验证码与本会话不匹配，请刷新")

    if not secrets.compare_digest(code.strip().upper(), record.get("code", "").upper()):
        await captchas.mark_used(captcha_id)
        raise CaptchaError("验证码错误，请重新输入")

    # 全部只读校验通过后原子烧码；烧码失败说明已被并发抢先使用
    if not await captchas.mark_used(captcha_id):
        raise BizError("验证码已使用，请刷新")
    log.debug("验证码校验通过: captcha_id={}", captcha_id)
