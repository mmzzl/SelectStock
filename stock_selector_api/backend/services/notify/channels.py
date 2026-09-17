"""通知渠道配置（JSON 文件存储，对齐 secvisual 的 sender 配置语义）。

- 四类渠道：企业微信/钉钉/飞书 机器人 webhook + 邮箱(SMTP)
- 敏感字段（SMTP 密码、钉钉/飞书 secret）AES-GCM 加密落盘，读取按类型脱敏
- 每个渠道实例有唯一 key，供订阅策略按 channel_key 引用
"""

import json
import os
import tempfile
from pathlib import Path

from config import settings
from core import security
from core.logger import log

# 渠道类型元数据：字段清单 + 需要加密的敏感字段
CHANNEL_TYPES: dict[str, dict] = {
    "wecom": {
        "label": "企业微信（群机器人）",
        "interface": "webhook",
        "fields": ["key", "name", "webhook_url"],
        "secret_fields": [],
        "required": ["key", "name", "webhook_url"],
    },
    "dingtalk": {
        "label": "钉钉（自定义机器人）",
        "interface": "webhook",
        "fields": ["key", "name", "webhook_url"],
        "secret_fields": ["secret"],
        "required": ["key", "name", "webhook_url"],
    },
    "feishu": {
        "label": "飞书（自定义机器人）",
        "interface": "webhook",
        "fields": ["key", "name", "webhook_url"],
        "secret_fields": ["secret"],
        "required": ["key", "name", "webhook_url"],
    },
    "email": {
        "label": "邮箱（SMTP）",
        "interface": "smtp",
        "fields": ["key", "name", "host", "port", "use_ssl", "username", "from_email", "from_name"],
        "secret_fields": ["password"],
        "required": ["key", "name", "host"],
    },
}

_MASK = "***encrypted***"


def _file() -> Path:
    return Path(settings.notify_channels_file)


def _load_raw() -> dict:
    p = _file()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8") or "{}")
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        log.error("读取通知渠道配置失败: {} {}", p, e)
        return {}


def _save_raw(data: dict) -> None:
    p = _file()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def list_all(channel_type: str | None = None) -> list[dict]:
    data = _load_raw()
    out = []
    for ctype, items in data.items():
        if channel_type and ctype != channel_type:
            continue
        for ch in items:
            out.append(public(ch))
    return out


def get_by_key(channel_key: str, channel_type: str | None = None) -> dict | None:
    entry = _load_entry(channel_key)
    if entry and (channel_type is None or entry.get("type") == channel_type):
        return _resolved(entry)
    return None


def _load_entry(channel_key: str) -> dict | None:
    """按 key 取原始（加密）配置。"""
    for items in _load_raw().values():
        for ch in items:
            if ch.get("key") == channel_key:
                return ch
    return None


def upsert(channel: dict) -> dict:
    """新增或更新渠道。敏感字段：空串/掩码=保持原值；否则加密落盘。key 全局唯一。"""
    ctype = channel.get("type")
    meta = CHANNEL_TYPES.get(ctype)
    if not meta:
        raise ValueError(f"未知渠道类型: {ctype}")
    key = (channel.get("key") or "").strip()
    if not key:
        raise ValueError("渠道 key 不能为空")

    data = _load_raw()
    # key 全局唯一（订阅策略按 channel_key 引用，跨类型重名会歧义）
    for t, chs in data.items():
        if t == ctype:
            continue
        if any(str(c.get("key")) == key for c in chs):
            raise ValueError(f"渠道 key 已存在且属于其他类型: {t}")

    items = data.setdefault(ctype, [])
    existing_idx = next((i for i, c in enumerate(items) if str(c.get("key")) == key), None)
    existing = items[existing_idx] if existing_idx is not None else None

    merged = dict(existing or {})
    # 普通字段 + 敏感字段一起遍历（敏感字段走加密，空/掩码=保留原值）
    all_fields = meta["fields"] + meta["secret_fields"]
    for f in all_fields:
        if f in channel:
            val = channel[f]
            if f in meta["secret_fields"]:
                # 空/掩码 => 保留旧密文；新值 => 加密
                if val not in ("", None, _MASK):
                    merged[f] = security.encrypt_secret(str(val))
            else:
                merged[f] = val
    # 类型本质上由存储目录决定，也记到条目上便于展示
    merged["type"] = ctype
    if "enabled" in channel:
        merged["enabled"] = bool(channel["enabled"])
    elif existing is not None:
        merged["enabled"] = bool(existing.get("enabled", True))
    else:
        merged["enabled"] = True

    if existing is None:
        items.append(merged)
    else:
        items[existing_idx] = merged
    _save_raw(data)
    return public(merged)


def delete(channel_key: str) -> bool:
    data = _load_raw()
    removed = False
    for items in data.values():
        keep = [c for c in items if c.get("key") != channel_key]
        if len(keep) != len(items):
            removed = True
            items[:] = keep
    if removed:
        _save_raw(data)
    return removed


def public(channel: dict) -> dict:
    """对外视图：敏感字段只保留是否已配置。"""
    out = dict(channel)
    meta = CHANNEL_TYPES.get(channel.get("type"), {})
    for f in meta.get("secret_fields", []):
        out[f] = bool(channel.get(f))
    return out


def _resolved(channel: dict) -> dict:
    """发送用视图：解出明文敏感字段。"""
    if not channel:
        return channel
    out = dict(channel)
    meta = CHANNEL_TYPES.get(channel.get("type"), {})
    for f in meta.get("secret_fields", []):
        enc = channel.get(f)
        out[f] = security.decrypt_secret(enc) if enc else ""
    return out
