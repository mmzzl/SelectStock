"""risk/sizing.py — 仓位额度与滑点预估：把「想买」换算成「能买多少、大概什么价成交」。

滑点模型（免费快照没有逐笔撮合，只能给可解释的估计，不假装精确）：
  预估滑点 = 半价差 + 冲击成本 + 信号价漂移
  - 半价差：实时快照有五档就取 (卖一-买一)/2；否则用最小变动价位半个 tick，再兜一个下限；
  - 冲击成本：`日波动 × √(下单额/日均成交额)`，即常说的平方根冲击，小单几乎为零、
    吃到日均成交额几个点时迅速变陡——这正是「小盘股容易滑点」要拦的形状；
  - 信号价漂移：策略看到的是推送里的那个数，行情有延迟，信号价与当前最新价的差
    就是「按信号价下不到单」的那部分成本，必须计进成本而不是当成免费午餐。

先按现价算额度、再按额度定量、最后用带滑点的价格复核，只走一轮：滑点本身依赖下单量，
迭代下去只会让结果依赖初始值，一轮足够，剩下的偏差远小于模型本身的不确定性。
"""

from __future__ import annotations

from dataclasses import dataclass, field

LOT = 100
BP = 1e4
TICK = 0.01


@dataclass
class Slippage:
    total_bp: float
    spread_bp: float
    impact_bp: float
    drift_bp: float
    fill_price: float | None       # 预估成交价（买 higher / sell lower）
    participation: float | None    # 下单额 / 日均成交额
    metrics: dict = field(default_factory=dict)


@dataclass
class PositionPlan:
    shares: int | None             # 建议股数（整手）；None = 算不出（缺价格）
    amount: float                  # 建议下单金额（元）
    cap_value: float               # 本次可用的额度（元）
    used_value: float              # 已占用（现有持仓市值）
    over_cap: bool                 # 请求的量超出额度
    reason: str = ""


def cap_value(capital: float, max_position_pct: float, held_value: float) -> float:
    """单票额度 = 总资金 × 上限比例 − 已持有市值；负数按 0 处理（已超限）。"""
    return max(0.0, capital * max_position_pct - max(0.0, held_value))


def round_lots(shares: float) -> int:
    return int(max(0, shares) // LOT) * LOT


def plan_position(price: float | None, capital: float, max_position_pct: float,
                  held_value: float, requested_shares: int | None,
                  enforce_cap: bool = True) -> PositionPlan:
    """把额度换算成整手股数；请求量超限就压到额度内（adjust），一点都买不了才算 block。

    `enforce_cap=False` 给卖出侧用：能卖多少由手里有多少决定，不受买入额度约束。
    """
    cap = cap_value(capital, max_position_pct, held_value) if enforce_cap else float("inf")
    if not price or price <= 0:
        return PositionPlan(None, 0.0, cap, held_value, False, "缺少成交价，无法折算股数")
    affordable = round_lots(cap / price) if enforce_cap else round_lots(held_value / price)
    if requested_shares is None:
        shares = affordable
        over = False
        reason = "" if shares else (
            f"额度 {cap:.0f} 元不足一手（现价 {price}）" if enforce_cap
            else f"没有可卖的整手持仓（现价 {price}）")
    else:
        shares = round_lots(min(requested_shares, affordable))
        over = enforce_cap and requested_shares > affordable
        reason = (f"请求 {requested_shares} 股超出单票额度，按 {shares} 股下单"
                  if over and shares else
                  (f"请求 {requested_shares} 股，单票额度只剩 {shares} 股" if over else ""))
    return PositionPlan(shares, shares * price, cap, held_value, over, reason)


def half_spread_bp(quote: dict, price: float) -> float:
    """半价差（万分比）。有五档用五档，没有就按半个 tick 折算，再兜住一个下限。"""
    bids, asks = quote.get("bids") or [], quote.get("asks") or []
    if bids and asks:
        gap = (asks[0][0] - bids[0][0]) / 2.0
        if gap > 0 and price > 0:
            return gap / price * BP
    return TICK / 2.0 / price * BP if price > 0 else 0.0


def impact_bp(order_amount: float, avg_amount: float | None, daily_vol_pct: float) -> float:
    if not avg_amount or avg_amount <= 0 or order_amount <= 0:
        return 0.0
    share = min(order_amount / avg_amount, 1.0)
    return daily_vol_pct / 100.0 * (share ** 0.5) * BP


def drift_bp(signal_price: float | None, quote_price: float | None) -> float:
    """信号价与最新价的偏离：行情延迟带来的那部分「看不见的成本」。"""
    if not signal_price or not quote_price or signal_price <= 0:
        return 0.0
    return abs(quote_price - signal_price) / signal_price * BP


def estimate(price: float | None, order_amount: float, *, quote: dict, avg_amount: float | None,
             daily_vol_pct: float, signal_price: float | None, action: str,
             min_spread_bp: float) -> Slippage:
    if not price or price <= 0:
        return Slippage(0.0, 0.0, 0.0, 0.0, None, None, {})
    spread = max(half_spread_bp(quote, price), min_spread_bp)
    impact = impact_bp(order_amount, avg_amount, daily_vol_pct)
    drift = drift_bp(signal_price, price)
    total = spread + impact + drift
    signed = total / BP
    fill = price * (1 + signed) if action == "buy" else price * (1 - signed)
    participation = order_amount / avg_amount if avg_amount and avg_amount > 0 else None
    return Slippage(round(total, 2), round(spread, 2), round(impact, 2), round(drift, 2),
                    round(fill, 4), participation,
                    {"spread_bp": round(spread, 2), "impact_bp": round(impact, 2),
                     "drift_bp": round(drift, 2), "total_slippage_bp": round(total, 2),
                     "participation": round(participation, 6) if participation else None,
                     "est_fill_price": round(fill, 4)})
