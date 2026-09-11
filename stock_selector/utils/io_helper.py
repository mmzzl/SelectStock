"""通用工具函数。"""

import json


def to_json(obj, ensure_ascii: bool = False) -> str:
    """将对象安全地序列化为 JSON 字符串。

    支持 SelectResult 单对象、list[SelectResult]（含 to_dict 的元素）以及常规类型。
    """
    if isinstance(obj, list):
        if obj and hasattr(obj[0], "to_dict"):
            obj = [r.to_dict() for r in obj]
    elif hasattr(obj, "to_dict"):
        obj = obj.to_dict()
    return json.dumps(obj, ensure_ascii=ensure_ascii, indent=2, default=str)


def parse_kwargs(raw: str | dict | None) -> dict:
    """解析外部传入的可选参数。

    支持：
    - None -> {}
    - dict -> 原样返回
    - 形如 "a=1;b=2" 的字符串 -> 拆分为 dict（值按数字/布尔/字符串推断）
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        result = {}
        for item in raw.split(";"):
            if not item.strip():
                continue
            key, _, value = item.partition("=")
            key = key.strip()
            value = value.strip()
            result[key] = _coerce(value)
        return result
    return {}


def _coerce(value: str):
    """把字符串值推断为数字/布尔/字符串。"""
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value
