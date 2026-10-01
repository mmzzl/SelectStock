"""回测参数白名单、缺省值与校验。

后端只做入参校验与投递（不落任何回测计算），worker 用同一份定义把 JSON 参数
组装成内核 CLI 的 Namespace。两处共用一份，避免加参数时只改一边导致
「前端能填、回测跑不到」。
"""
from __future__ import annotations

import re

from core.exceptions import BizError

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 缺省值与 scripts/backtest_main.py 的 argparse 默认值保持一致
DEFAULTS: dict = {
    "start": "2025-09-01",
    "end": "2026-09-01",
    "capital": 100000.0,
    "max_positions": 5,
    "position_cap_pct": 0.2,
    "target_amount": 15000.0,
    "commission_rate": 0.00025,
    "commission_min": 5.0,
    "stamp_tax_rate": 0.0005,
    "strategy": None,        # 空 = 走 MongoDB trading_rules 规则回测
    "universe": 0,           # 仅 strategy 模式生效，0 = 全市场
    "max_hold_days": 10,
    "stop_loss_pct": 0.05,
    "take_profit_pct": 0.0,
    "no_save": False,
}

# 数值参数：(类型, 下界, 上界)，None 表示该侧不校验
_NUM_SPEC: dict = {
    "capital": (float, 1000.0, 1_000_000_000.0),
    "max_positions": (int, 1, 50),
    "position_cap_pct": (float, 0.01, 1.0),
    "target_amount": (float, 100.0, 100_000_000.0),
    "commission_rate": (float, 0.0, 0.01),
    "commission_min": (float, 0.0, 1000.0),
    "stamp_tax_rate": (float, 0.0, 0.01),
    "universe": (int, 0, 6000),
    "max_hold_days": (int, 0, 250),
    "stop_loss_pct": (float, 0.0, 0.5),
    "take_profit_pct": (float, 0.0, 1.0),
}


def form_spec() -> dict:
    """给前端表单的单一事实来源：缺省值 + 数值区间，避免页面自己硬编码一套边界。"""
    return {
        "defaults": dict(DEFAULTS),
        "numeric": {
            key: {"type": "int" if typ is int else "float", "min": lo, "max": hi}
            for key, (typ, lo, hi) in _NUM_SPEC.items()
        },
    }


def merge(params: dict | None) -> dict:
    """缺省值 + 白名单内的非空覆盖，供 worker 组装内核参数。"""
    out = dict(DEFAULTS)
    for key, value in (params or {}).items():
        if key in DEFAULTS and value is not None:
            out[key] = value
    return out


def validate(raw: dict | None) -> dict:
    """校验前端提交的参数，返回归一化后的字段（未提交的键不出现）。"""
    params = raw or {}
    unknown = sorted(k for k in params if k not in DEFAULTS)
    if unknown:
        raise BizError(f"未知回测参数: {', '.join(unknown)}", code=400, status_code=400)

    out: dict = {}
    for key, value in params.items():
        if value is None:
            continue
        if key in ("start", "end"):
            out[key] = _date(key, value)
        elif key == "strategy":
            name = str(value).strip()
            if len(name) > 64:
                raise BizError("策略名过长", code=400, status_code=400)
            out[key] = name
        elif key == "no_save":
            out[key] = bool(value)
        else:
            typ, lo, hi = _NUM_SPEC[key]
            out[key] = _num(key, value, typ, lo, hi)

    effective = merge(out)
    _check_window(effective)
    _check_position_sizing(effective)
    return out


def _date(key: str, value) -> str:
    text = str(value).strip()
    if not DATE_RE.match(text):
        raise BizError(f"{key} 需为 YYYY-MM-DD 格式", code=400, status_code=400)
    return text


def _num(key: str, value, typ: type, lo, hi):
    try:
        number = typ(value)
    except (TypeError, ValueError):
        raise BizError(f"{key} 需为数字", code=400, status_code=400) from None
    if lo is not None and number < lo:
        raise BizError(f"{key} 不能小于 {lo:g}", code=400, status_code=400)
    if hi is not None and number > hi:
        raise BizError(f"{key} 不能大于 {hi:g}", code=400, status_code=400)
    return number


def _check_window(params: dict) -> None:
    if params["start"] >= params["end"]:
        raise BizError("起始日期需早于结束日期", code=400, status_code=400)


def _check_position_sizing(params: dict) -> None:
    """单票上限按总资产算：单笔金额贴边或超出时，净值一回撤成交就会被上限静默拒绝，
    回测看起来像「没有信号」。CLI 对此只打警告，后台提交则直接拒绝，免得产出空结果。
    """
    cap = params["capital"] * params["position_cap_pct"]
    if params["target_amount"] >= cap:
        raise BizError(
            f"单笔买入金额 {params['target_amount']:g} 需小于单票上限 "
            f"{cap:g}（初始资金 × 单票仓位上限）",
            code=400,
            status_code=400,
        )
