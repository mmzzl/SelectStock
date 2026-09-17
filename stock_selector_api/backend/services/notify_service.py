"""通知服务：订阅策略加载/保存、事件发送与节流、模板渲染、测试发送。

策略 JSON 结构对齐 secvisual 订阅策略（alarm_policy.json）语义：
  {
    "is_on": 总开关,
    "events": { "<事件码>": {"is_on": 开关, "interval": {"value": n, "unit": "m|h|d"}, "subject": 可选}, ... },
    "channels": {
      "wecom|dingtalk|feishu":{"is_on":bool,"channel_key":渠道key,"template":可选},
      "email": {"is_on":bool,"channel_key":...,"to":["回收件人"],"template":可选}
    }
  }
"""

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from config import settings
from core.exceptions import BizError
from core.logger import log
from services.notify import channels, events
from services.notify import senders

DEFAULT_POLICY: dict = {
    "is_on": False,
    "events": {
        code: {"is_on": False, "interval": {"value": 0, "unit": "m"}}
        for code in sorted(events.EVENT_CODES)
    },
    "channels": {
        "wecom": {"is_on": False, "channel_key": ""},
        "dingtalk": {"is_on": False, "channel_key": ""},
        "feishu": {"is_on": False, "channel_key": ""},
        "email": {"is_on": False, "channel_key": "", "to": []},
    },
}


def _policy_file() -> Path:
    return Path(settings.notify_policy_file)


def _state_file() -> Path:
    return Path(settings.notify_state_file)


def load_policy() -> dict:
    p = _policy_file()
    if not p.exists():
        return json.loads(json.dumps(DEFAULT_POLICY))
    try:
        data = json.loads(p.read_text(encoding="utf-8") or "{}")
        if not isinstance(data, dict):
            return json.loads(json.dumps(DEFAULT_POLICY))
        return _merge_defaults(data)
    except (json.JSONDecodeError, OSError) as e:
        log.error("读取订阅策略失败: {} {}", p, e)
        return json.loads(json.dumps(DEFAULT_POLICY))


def save_policy(policy: dict) -> dict:
    merged = _merge_defaults(policy)
    p = _policy_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return merged


def _merge_defaults(data: dict) -> dict:
    out = json.loads(json.dumps(DEFAULT_POLICY))
    out["is_on"] = bool(data.get("is_on", False))
    evs = out["events"]
    for code, cfg in (data.get("events") or {}).items():
        if code in evs:
            evs[code]["is_on"] = bool(cfg.get("is_on", False))
            if isinstance(cfg.get("interval"), dict):
                evs[code]["interval"] = {
                    "value": int(cfg["interval"].get("value", 0) or 0),
                    "unit": str(cfg["interval"].get("unit", "m") or "m") if (cfg["interval"].get("value") or 0) > 0 else "m",
                }
            if cfg.get("subject"):
                evs[code]["subject"] = str(cfg["subject"])
    chs = out["channels"]
    for ctype, cfg in (data.get("channels") or {}).items():
        if ctype in chs:
            chs[ctype]["is_on"] = bool(cfg.get("is_on", False))
            chs[ctype]["channel_key"] = str(cfg.get("channel_key") or "")
            if cfg.get("template"):
                chs[ctype]["template"] = str(cfg["template"])
            if ctype == "email" and isinstance(cfg.get("to"), list):
                chs[ctype]["to"] = [str(x).strip() for x in cfg["to"] if str(x).strip()]
    return out


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


def _throttle_ok(event_code: str, event_cfg: dict) -> bool:
    interval = _interval_seconds(event_cfg.get("interval") or {})
    if interval <= 0:
        return True
    state = _load_state()
    last = state.get(event_code, {}).get("last", 0)
    return (datetime.now(timezone.utc).timestamp() - float(last)) >= interval


def _mark_sent(event_code: str) -> None:
    state = _load_state()
    state.setdefault(event_code, {})["last"] = datetime.now(timezone.utc).timestamp()
    _save_state(state)


# ----------------------------------------------------------------------
# 发送
# ----------------------------------------------------------------------

async def emit(event_code: str, **payload) -> None:
    """订阅事件入口：命中已启用的事件(且通过节流) → 分发到策略启用的渠道。任何失败仅记日志不抛。"""
    if not settings.notify_enabled:
        return
    policy = load_policy()
    if not policy.get("is_on"):
        return
    event_cfg = policy.get("events", {}).get(event_code)
    if not event_cfg or not event_cfg.get("is_on"):
        return
    if not _throttle_ok(event_code, event_cfg):
        log.debug("通知节流跳过: {}", event_code)
        return

    subject, content = render_default(event_code, payload)
    subject = event_cfg.get("subject") or subject
    sent_any = False
    for ctype, ch_cfg in policy.get("channels", {}).items():
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
            log.info("通知已发送: 事件={} 渠道={}({})", event_code, ctype, ch_cfg.get("channel_key"))
        except Exception as e:
            log.error("通知发送失败: 事件={} 渠道={}({}): {}", event_code, ctype, ch_cfg.get("channel_key"), e)
    if sent_any:
        _mark_sent(event_code)
    else:
        log.debug("通知无可用渠道: 事件={}", event_code)


async def test_send(channel_key: str, text: str, to: list[str] | None = None) -> None:
    """向指定渠道发一条测试消息（邮箱渠道需传收件人）。"""
    for ctype in channels.CHANNEL_TYPES:
        channel = channels.get_by_key(channel_key, ctype)
        if channel:
            await senders.send(ctype, channel, f"[{settings.app_name}] 测试通知",
                               text or "这是一条测试消息", to=to)
            return
    raise BizError("渠道不存在")
