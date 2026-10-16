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
    # 规则集快照：页面「回测」把当前生效的卖出/加仓串带进这一次回测（只在 rules 模式生效，
    # 即 strategy 为空）。带着串跑策略模式会被内核静默忽略，所以那种组合在 validate 里直接拒。
    "rule_set_id": "",
    "sell_rule": "",
    "add_rule": "",
}

# 字符串参数：{键: 长度上限}
_STR_SPEC: dict = {"rule_set_id": 36, "sell_rule": 2000, "add_rule": 2000}

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
        elif key in _STR_SPEC:
            out[key] = _text(key, value)
        elif key == "no_save":
            out[key] = bool(value)
        else:
            typ, lo, hi = _NUM_SPEC[key]
            out[key] = _num(key, value, typ, lo, hi)

    effective = merge(out)
    _check_window(effective)
    _check_position_sizing(effective)
    _check_rule_snapshot(effective)
    return out


def _date(key: str, value) -> str:
    text = str(value).strip()
    if not DATE_RE.match(text):
        raise BizError(f"{key} 需为 YYYY-MM-DD 格式", code=400, status_code=400)
    return text


_RULE_CN = {"sell_rule": "卖出规则串", "add_rule": "加仓规则串"}


def _text(key: str, value) -> str:
    from core import rule_grammar

    text = str(value).strip()
    if len(text) > _STR_SPEC[key]:
        raise BizError(f"{key} 过长（上限 {_STR_SPEC[key]} 字符）", code=400, status_code=400)
    if key in _RULE_CN and text:
        # 当场校验语法：坏串存进内核不会报错，只会让那条规则永远不触发（RuleEvaluator 静默 False）
        mod = rule_grammar.rule_expr()
        try:
            mod.validate(text)
        except mod.RuleSyntaxError as exc:
            raise BizError(f"{_RULE_CN[key]}不合法：{exc}", code=400, status_code=400) from None
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


def _check_rule_snapshot(params: dict) -> None:
    """规则集串只有「规则模式」（不选策略）才消费。

    选了选股策略时出场走 max_hold_days/止损，内核根本不读 sell/add 表达式；
    那种组合下页面却写着「按规则集退出」，产出的赢比就不能拿去当规则集的依据。
    """
    if params["strategy"] and (params["sell_rule"] or params["add_rule"]):
        raise BizError(
            "策略模式的出场由持有天数与止损决定，不读规则集的卖出/加仓串；"
            "要验规则集请先清空策略",
            code=400,
            status_code=400,
        )
