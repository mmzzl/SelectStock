"""订阅策略：多条策略的存储与规范化（JSON 文件，与渠道配置同层）。

每条策略 = 名称 + 自身开关 + 事件订阅（各自节流）+ 渠道绑定（渠道实例/收件人/模板）。

`emit()` 侧只读 `enabled_policies()`，节流按 (策略 id, 事件码) 记账，
因此两条策略订阅同一事件时各走各的间隔，不会互相压制。
"""

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from config import settings
from core.logger import log
from services.notify import channels, events

_UNITS = ("m", "h", "d")


def _file() -> Path:
    return Path(settings.notify_policies_file)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_raw() -> list[dict]:
    p = _file()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8") or "[]")
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as e:
        log.error("读取订阅策略失败: {} {}", p, e)
        return []


def _save_raw(items: list[dict]) -> None:
    p = _file()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def normalize(data: dict, policy_id: str) -> dict:
    """校验并补全：事件码与渠道类型必须在目录内，未订阅的项落成关闭占位。

    脏键直接拒而不是忽略——策略静默少订阅一个事件比报错更难查。
    """
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("策略名称不能为空")

    ev_in = data.get("events") or {}
    unknown_ev = sorted(str(c) for c in ev_in if c not in events.EVENT_CODES)
    if unknown_ev:
        raise ValueError("未知事件码: " + ", ".join(unknown_ev))
    ch_in = data.get("channels") or {}
    unknown_ch = sorted(str(t) for t in ch_in if t not in channels.CHANNEL_TYPES)
    if unknown_ch:
        raise ValueError("未知渠道类型: " + ", ".join(unknown_ch))

    out_events: dict[str, dict] = {}
    for code in sorted(events.EVENT_CODES):
        cfg = ev_in.get(code) or {}
        raw_interval = cfg.get("interval") or {}
        try:
            value = int(raw_interval.get("value") or 0)
        except (TypeError, ValueError):
            raise ValueError(f"事件 {code} 的发送间隔必须是整数")
        if value < 0:
            value = 0
        unit = str(raw_interval.get("unit") or "m")
        out_events[code] = {
            "is_on": bool(cfg.get("is_on")),
            # 间隔 0 表示不节流，单位一律归到分钟，避免存下无意义的 h/d
            "interval": {"value": value, "unit": unit if unit in _UNITS and value > 0 else "m"},
        }
        if cfg.get("subject"):
            out_events[code]["subject"] = str(cfg["subject"])[:200]

    out_channels: dict[str, dict] = {}
    for ctype in channels.CHANNEL_TYPES:
        cfg = ch_in.get(ctype) or {}
        entry = {
            "is_on": bool(cfg.get("is_on")),
            "channel_key": str(cfg.get("channel_key") or ""),
        }
        if ctype == "email":
            entry["to"] = [str(x).strip() for x in (cfg.get("to") or []) if str(x).strip()]
        if cfg.get("template"):
            entry["template"] = str(cfg["template"])[:2000]
        out_channels[ctype] = entry

    return {
        "id": policy_id,
        "name": name,
        "is_on": bool(data.get("is_on", True)),
        "events": out_events,
        "channels": out_channels,
    }


def list_all() -> list[dict]:
    return _load_raw()


def get(policy_id: str) -> dict | None:
    return next((p for p in _load_raw() if p.get("id") == policy_id), None)


def enabled_policies() -> list[dict]:
    """`emit()` 用：只返回自身启用的策略。"""
    return [p for p in _load_raw() if p.get("is_on")]


def create(data: dict) -> dict:
    items = _load_raw()
    policy = normalize(data, uuid.uuid4().hex[:12])
    now = _now()
    policy["created_at"] = now
    policy["updated_at"] = now
    items.append(policy)
    _save_raw(items)
    return policy


def update(policy_id: str, data: dict) -> dict | None:
    items = _load_raw()
    idx = next((i for i, p in enumerate(items) if p.get("id") == policy_id), None)
    if idx is None:
        return None
    policy = normalize(data, policy_id)
    policy["created_at"] = items[idx].get("created_at") or _now()
    policy["updated_at"] = _now()
    items[idx] = policy
    _save_raw(items)
    return policy


def delete(policy_id: str) -> bool:
    items = _load_raw()
    keep = [p for p in items if p.get("id") != policy_id]
    if len(keep) == len(items):
        return False
    _save_raw(keep)
    return True
