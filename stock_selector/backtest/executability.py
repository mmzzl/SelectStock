"""成交可执行性：A 股哪些单子当天根本成交不了。

这里是**判据的唯一真源**，两条链路都从这里取数：
- 逐笔扫描 `scripts/profit_scan.py`（次一交易日开盘买入、按止盈/止损/到期卖出）；
- 组合回测 `stock_selector/backtest/engine.py`（当日收盘成交、有仓位上限与资金循环）。

两条链路的**成交时点不同**（一个次日开盘、一个当日收盘），所以判据的"看哪根价"不同，
但"什么情况下成交不了"是同一件事：

1. **涨停买不进**：涨幅已经贴着涨停线（差 `LIMIT_TOL_PP` 以内就当封住了），
   挂单排在队尾成交不了。逐笔扫描看的是次日开盘相对前收的跳空，组合回测看当日收盘涨幅。
2. **一字板卖不掉**：全天最高=最低 ⇒ 没有对手盘，出不掉。方向不分涨跌——
   一字涨停理论上卖得进，但库里的日线看不出封单厚薄，两边都按卖不掉处理，
   与逐笔扫描同一口径（宁可保守，不要把撮合假设的钱当成行情的钱）。
3. **停牌押着等**：当日没有行情行 ⇒ 既买不进也卖不掉。买入侧直接放弃这笔；
   卖出侧仓位还在手上，只能顺延到后面第一个有对手盘的交易日。

顺移上限 `REPAIR_MAX_DAYS` 只在逐笔扫描里用（超过就当这笔没成交、从分母里丢），
组合回测不能丢仓位——真实账户只能一直押着等，所以引擎不设上限，只把押了多少天记出来。
"""
from __future__ import annotations

import math

from stock_selector.features.base_columns import board_limit_threshold

LIMIT_TOL_PP = 0.05        # 距涨停线 0.05pp 以内视为「已封住」
REPAIR_MAX_DAYS = 10       # 逐笔扫描：卖不掉最多顺移几天，超过判「没成交」


def is_one_way_board(high, low) -> bool:
    """一字板（最高=最低）：当天没有对手盘。缺高低价时返回 False（无从判定，不拦单）。"""
    if not _finite(high) or not _finite(low):
        return False
    return float(high) == float(low)


def day_change_pct(row: dict, prev_close=None) -> float | None:
    """当日涨跌幅（%）：日线自带的 pct 优先，缺了就按昨收折算，昨收也没有返回 None。

    None 表示"无法判定"，调用方要把它记进 `unknown` 计数而不是当成 0——
    静默当 0 会让"这轮回测根本没校验涨跌停"看不出来。
    """
    pct = row.get("pct")
    if _finite(pct):
        return float(pct)
    close = row.get("close")
    if _finite(close) and _finite(prev_close) and float(prev_close) > 0:
        return (float(close) / float(prev_close) - 1.0) * 100.0
    return None


def buy_blocked(code: str, pct, high=None, low=None) -> str | None:
    """买不进的原因，None = 可以买。

    `pct` 是成交时点的涨跌幅：逐笔扫描传「次日开盘相对前收的跳空」，组合回测传「当日收盘涨幅」。
    贴着涨停线（含 `LIMIT_TOL_PP` 容差）就判买不进；一字板单独记原因，
    因为「炸板还买得进」和「整块封死」在归因里是两回事。
    """
    if not _finite(pct):
        return None
    if float(pct) < board_limit_threshold(code) - LIMIT_TOL_PP:
        return None
    return "sealed" if is_one_way_board(high, low) else "at_limit"


def sell_blocked(high, low) -> str | None:
    """卖不掉的原因，None = 可以卖。一字板当天没有对手盘（停牌由调用方按「无行情行」判）。"""
    return "one_way_board" if is_one_way_board(high, low) else None


def _finite(x) -> bool:
    return x is not None and isinstance(x, (int, float)) and math.isfinite(float(x))
