"""rule_trigger.py — 把规则里的收盘价当自变量，反算「价格触发线」。

判定内核是布尔表达式（RuleEvaluator），这里不复制任何阈值常量：只在 close 这根
一维轴上采样求值，找离当前收盘价最近的翻转点。其余指标保持基准日取值不动，所以
反算出来的线和 `judge_holding` 的判定永远同口径——同一条表达式、同一个求值器。
"""

from __future__ import annotations

import math

from .rule_eval import RuleEvaluator

_eval = RuleEvaluator()

# close 的扫描倍率区间与采样点数。均线/箱线类触发线几乎都落在 ±50% 内，
# 0.3x~3x 给极端规则留余量；先粗扫找翻转括号，再二分收敛到分。
_LO_FACTOR, _HI_FACTOR, _GRID = 0.3, 3.0, 48
_BISECT_STEPS = 24


def _at_close(row: dict, x: float) -> dict:
    """把 close（及其别名 price）换成 x，其余列原样保留。"""
    out = dict(row)
    out["close"] = x
    if "price" in out:
        out["price"] = x
    return out


def nearest_price_trigger(rule: str, row: dict) -> tuple[float, str] | None:
    """返回 (触发价, 方向)：方向 "down" 表示价格往下穿过该线会翻转判定，"up" 往上穿过翻转。

    反算不出来时返回 None，三种情况：规则不含价格项（例如只看 rsi14）、当前收盘价
    缺失或非正、扫描区间内判定值始终不变（价格怎么动都不触发）。
    """
    if not rule or not isinstance(rule, str) or not isinstance(row, dict):
        return None
    try:
        base = float(row["close"])
    except (KeyError, TypeError, ValueError):
        return None
    if not math.isfinite(base) or base <= 0:
        return None

    xs = [base * math.exp(math.log(_LO_FACTOR) + (math.log(_HI_FACTOR) - math.log(_LO_FACTOR))
                          * i / (_GRID - 1)) for i in range(_GRID)]
    vals = [_eval.eval(rule, _at_close(row, x)) for x in xs]
    brackets = [i for i in range(1, _GRID) if vals[i] != vals[i - 1]]
    if not brackets:
        return None

    # 多个翻转（规则里既有上破又有下破）时取离当前价最近的那个：它就是现有判定的边界
    i = min(brackets, key=lambda k: min(abs(xs[k] - base), abs(xs[k - 1] - base)))
    lo, hi, lo_val = xs[i - 1], xs[i], vals[i - 1]
    for _ in range(_BISECT_STEPS):
        mid = (lo + hi) / 2
        if _eval.eval(rule, _at_close(row, mid)) == lo_val:
            lo = mid
        else:
            hi = mid
    price = (lo + hi) / 2
    return round(price, 2), ("down" if price < base else "up")
