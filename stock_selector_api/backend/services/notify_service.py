"""通知服务：订阅事件分发与节流、模板渲染、测试发送。

策略结构（多条，见 services/notify/policies.py）：
  {
    "id": 策略 id, "name": 名称, "is_on": 该策略开关,
    "events": { "<事件码>": {"is_on": 开关, "interval": {"value": n, "unit": "m|h|d"}, "subject": 可选} },
    "channels": {
      "wecom|dingtalk|feishu": {"is_on": bool, "channel_key": 渠道key, "template": 可选},
      "email": {"is_on": bool, "channel_key": ..., "to": ["回收件人"], "template": 可选}
    }
  }
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from config import settings
from core.exceptions import BizError
from core.logger import log
from services.notify import channels, events, policies
from services.notify import senders


def _state_file() -> Path:
    return Path(settings.notify_state_file)


# ----------------------------------------------------------------------
# 渲染与节流
# ----------------------------------------------------------------------

_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def render_default(event_code: str, payload: dict) -> tuple[str, str]:
    """返回 (subject, content)。默认模板：事件名 + 时间 + 明细 k=v。"""
    ts = datetime.now(timezone.utc).astimezone().strftime(_TIME_FORMAT)
    lines = [f"[{settings.app_name}] {events.event_label(event_code)}", f"时间: {ts}"]
    if payload:
        lines.append("明细:")
        for k, v in payload.items():
            lines.append(f"  {k}: {v}")
    subject = f"[{settings.app_name}] {events.event_label(event_code)}"
    return subject, "\n".join(lines)


def render(custom_template: str, event_code: str, payload: dict) -> str:
    """策略里配置的自定义模板（支持 {event}/{time}/{payload 键}）。失败回退默认正文。"""
    if not custom_template:
        _, content = render_default(event_code, payload)
        return content
    try:
        ts = datetime.now(timezone.utc).astimezone().strftime(_TIME_FORMAT)
        ctx = {"event": events.event_label(event_code), "time": ts, **{str(k): str(v) for k, v in payload.items()}}
        return custom_template.format(**ctx)
    except (KeyError, IndexError, ValueError):
        _, content = render_default(event_code, payload)
        return content


def _interval_seconds(interval: dict) -> int:
    value = int(interval.get("value", 0) or 0)
    if value <= 0:
        return 0
    unit = str(interval.get("unit", "m") or "m")
    return value * {"m": 60, "h": 3600, "d": 86400}.get(unit, 60)


def _load_state() -> dict:
    p = _state_file()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    p = _state_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, p)


def _throttle_ok(policy_id: str, event_code: str, event_cfg: dict) -> bool:
    interval = _interval_seconds(event_cfg.get("interval") or {})
    if interval <= 0:
        return True
    state = _load_state()
    last = state.get(policy_id, {}).get(event_code, {}).get("last", 0)
    return (datetime.now(timezone.utc).timestamp() - float(last)) >= interval


def _mark_sent(policy_id: str, event_code: str) -> None:
    state = _load_state()
    state.setdefault(policy_id, {})[event_code] = {
        "last": datetime.now(timezone.utc).timestamp()
    }
    _save_state(state)


# ----------------------------------------------------------------------
# 发送
# ----------------------------------------------------------------------

async def emit(event_code: str, **payload) -> None:
    """订阅事件入口：遍历启用中的策略，命中该策略订阅的事件即按它的渠道投递。

    一条策略命中不影响其他策略（节流各记各的），所以同一事件可以分不同收件人群。
    任何失败仅记日志不抛——通知是旁路，不能拖垮业务请求。
    """
    if not settings.notify_enabled:
        return
    for policy in policies.enabled_policies():
        await _emit_to_policy(policy, event_code, **payload)


async def _emit_to_policy(policy: dict, event_code: str, **payload) -> None:
    policy_id = str(policy.get("id") or "")
    event_cfg = (policy.get("events") or {}).get(event_code)
    if not event_cfg or not event_cfg.get("is_on"):
        return
    if not _throttle_ok(policy_id, event_code, event_cfg):
        log.debug("通知节流跳过: 策略={} 事件={}", policy.get("name") or policy_id, event_code)
        return

    subject, content = render_default(event_code, payload)
    subject = event_cfg.get("subject") or subject
    sent_any = False
    for ctype, ch_cfg in (policy.get("channels") or {}).items():
        if not ch_cfg.get("is_on"):
            continue
        channel = channels.get_by_key(ch_cfg.get("channel_key") or "", ctype)
        if not channel or not channel.get("enabled", True):
            continue
        try:
            text = render(ch_cfg.get("template"), event_code, payload)
            to = ch_cfg.get("to") if ctype == "email" else None
            await senders.send(ctype, channel, subject, text, to=to)
            sent_any = True
            log.info("通知已发送: 策略={} 事件={} 渠道={}({})", policy.get("name") or policy_id,
                     event_code, ctype, ch_cfg.get("channel_key"))
        except Exception as e:
            log.error("通知发送失败: 策略={} 事件={} 渠道={}({}): {}", policy.get("name") or policy_id,
                      event_code, ctype, ch_cfg.get("channel_key"), e)
    if sent_any:
        _mark_sent(policy_id, event_code)
    else:
        log.debug("通知无可用渠道: 策略={} 事件={}", policy.get("name") or policy_id, event_code)


async def test_send(channel_key: str, text: str, to: list[str] | None = None) -> None:
    """向指定渠道发一条测试消息（邮箱渠道需传收件人）。"""
    for ctype in channels.CHANNEL_TYPES:
        channel = channels.get_by_key(channel_key, ctype)
        if channel:
            await senders.send(ctype, channel, f"[{settings.app_name}] 测试通知",
                               text or "这是一条测试消息", to=to)
            return
    raise BizError("渠道不存在")
