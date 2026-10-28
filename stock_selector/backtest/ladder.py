"""阶梯加仓纪律：四笔 40/30/20/10、间距 k×ATR、止损挂最近一笔成交价（批次 T，docs §33）。

口径的真源是 §33.1 那段用户原话，实现照 §33.2 的表逐条落：

- 金额按**单票目标成本**的四成/三成/两成/一成切，之和 = 100%，走满四档就不再往里加
  （仓位不许越过目标成本，这是这条纪律唯一的硬顶）；
- 下一档触发价 = 最近一笔成交价 + k×ATR14（k 主臂 2.0、记录臂 1.5），**成交当时锁死**，
  不跟着后面的 ATR 漂——它得是一条能挂得出去的价；
- 止损线 = 最近一笔成交价 ×(1−5%)，只上移不回退：在更低价加仓（摊低）不许把已经抬起来的
  线放低，否则「锁住浮盈」这句话就没了；
- 跳空高开越过触发价时按**当日开盘**成交（条件单挂在上面，跳空只会更差），同日既触止损
  又触加仓时按止损算（§33.4 写死的保守序）。

这个模块只有规则本身：不读行情、不碰账户、不 import qlib。回测读数（`scripts/qlib_ladder_ab.py`）
和以后可能接的线上诊断共用同一个状态机，两边各写一份迟早会对不上。

ATR 复用 `features/chips.atr14`（与 §32 那条「止损=买入−2×ATR」同一个函数、同一套「停牌/缺数
不进统计」的过滤），所以 `bars` 要带 `open/high/low/close/amount` 五列并按时间升序。
"""
from __future__ import annotations

import numpy as np

from stock_selector.features.chips import ATR_WINDOW, atr14

WEIGHTS = (0.40, 0.30, 0.20, 0.10)   # 四笔占目标成本的比例，之和必须是 1
STOP_PCT = 0.05                       # 止损 = 最近成交价 ×(1−STOP_PCT)
K_MAIN = 2.0                          # 主臂间距
K_ALT = 1.5                           # 记录臂间距（§33.3 第 4 条：不许看完数再挑）
BUY_FEE = 0.0005                      # 与 §26 四臂同一对常数（qlib 回测的 open_cost/close_cost）
SELL_FEE = 0.0015


def add_amount(step: int, target_cost: float, weights=WEIGHTS) -> float:
    """第 `step` 笔（1 起）该投多少钱；阶梯走完（超过档数）给 0，不是「再挤一点」。"""
    if step < 1 or step > len(weights):
        return 0.0
    return float(target_cost) * weights[step - 1]


def rung_price(fill_price, atr, k: float = K_MAIN):
    """下一档加仓触发价。波动算不出来（NaN/0）或价格不真实时给 None = 这一档不给。"""
    if fill_price is None or atr is None:
        return None
    fill, vol = float(fill_price), float(atr)
    if not np.isfinite(fill) or not np.isfinite(vol) or fill <= 0 or vol <= 0:
        return None
    return fill + float(k) * vol


def raise_stop(prev_stop, fill_price: float, stop_pct: float = STOP_PCT) -> float | None:
    """跟着成交价上移的止损线：新低线不采信，线只会往上走。`stop_pct <= 0` = 这一侧整条关掉
    （C 归因臂要的就是「按阶梯加仓但不设止损」，不是退化成「跌破成交价就卖」）。"""
    if stop_pct <= 0:
        return None
    line = float(fill_price) * (1.0 - stop_pct)
    return line if prev_stop is None else max(float(prev_stop), line)


def simulate_ladder(bars, *, entry_at: int, target_cost: float, k: float = K_MAIN,
                    stop_pct: float = STOP_PCT, hold: int = 10,
                    buy_fee: float = BUY_FEE, sell_fee: float = SELL_FEE) -> dict | None:
    """单票走一遍阶梯，返回成交明细与扣费后收益；算不了就返回 None（不编一条收益）。

    `bars` 升序、含 open/high/low/close/amount；`entry_at` 是入场那根的整数位置（该日收盘买第一笔，
    所以它之前必须够 ATR_WINDOW+1 根），右端必须留满 `hold` 根——**断档时宁可不给读数**，
    否则「拿满 10 日」和「只拿到 3 日」会被混进同一个平均值里。
    """
    if entry_at < ATR_WINDOW or len(bars) < entry_at + hold + 1:
        return None
    entry_price = float(bars["close"].iloc[entry_at])
    atr = atr14(bars.iloc[:entry_at + 1])
    if not np.isfinite(entry_price) or entry_price <= 0 or not np.isfinite(atr):
        return None

    fills = [{"step": 1, "price": entry_price, "amount": add_amount(1, target_cost)}]
    stop_line = raise_stop(None, entry_price, stop_pct)
    rung = rung_price(entry_price, atr, k)
    exit_at = entry_at + hold
    exit_price = exit_reason = None

    for day in range(entry_at + 1, exit_at + 1):
        bar = bars.iloc[day]
        if stop_line is not None and float(bar["low"]) <= stop_line:
            exit_price, exit_reason, exit_at = stop_line, "stop", day
            break
        if rung is not None and len(fills) < len(WEIGHTS) and float(bar["high"]) >= rung:
            price = max(float(bar["open"]), rung)
            fills.append({"step": len(fills) + 1, "price": price,
                          "amount": add_amount(len(fills) + 1, target_cost)})
            stop_line = raise_stop(stop_line, price, stop_pct)
            rung = rung_price(price, atr14(bars.iloc[:day + 1]), k)

    if exit_price is None:
        exit_price, exit_reason = float(bars["close"].iloc[exit_at]), "hold"
    invested_cost = sum(f["amount"] for f in fills)
    shares = sum(f["amount"] / f["price"] for f in fills)
    buy_fees = invested_cost * buy_fee
    invested = invested_cost + buy_fees
    proceeds = shares * exit_price * (1.0 - sell_fee)
    return {"fills": fills, "steps": len(fills), "stop_line": stop_line,
            "exit_at": exit_at, "exit_price": exit_price, "exit_reason": exit_reason,
            "shares": shares, "invested": invested, "buy_fees": buy_fees,
            "proceeds": proceeds, "ret_pct": proceeds / invested - 1.0}
