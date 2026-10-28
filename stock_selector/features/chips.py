"""筹码成本分布与由它推出的三档价位（买入区间 / 第一止盈 / 波动率止损）。

口径（用户点名的一条，2026-10-28）：
- 支撑价 = 近 60 日筹码**主峰下沿** → 目标买入区间；
- 压力价 = 近 60 日筹码**主峰上沿** → 第一止盈卖出价；
- 止损价 = 买入价 − 2×ATR14。

本机没有独立的筹码分布实现，这里用**已有的日线**合成：每日成交额均匀摊到当天 [low, high]
覆盖的价位桶上，近 60 日累加就是成本分布；最密的连续段（权重不低于峰桶一半的邻桶）就是主峰。

防前瞻：全部函数只吃调用方切好的 `bars`（信号日及之前，按时间升序），内部不取任何未来行。
数据源由调用方决定（qlib bin 面板或 `data/stock_kline/{code}.parquet`），本模块不读文件。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

WINDOW = 60          # 筹码窗口：近 60 个交易日
BINS = 40            # 价位分桶数
PEAK_RATIO = 0.5     # 主峰向两侧扩展的下限：邻桶权重 ≥ 峰桶 × 此比例才算同一个峰
ATR_WINDOW = 14      # 波动率窗口
STOP_ATR = 2.0       # 止损 = 买入价 − STOP_ATR × ATR


def _usable(bars: pd.DataFrame) -> pd.DataFrame:
    df = bars.dropna(subset=["low", "high", "close", "amount"])
    df = df[(df["amount"] > 0) & (df["high"] >= df["low"]) & (df["low"] > 0)]
    return df


def cost_profile(bars: pd.DataFrame, window: int = WINDOW, bins: int = BINS) -> pd.Series:
    """近 window 根的成本分布：索引=价位桶中心，值=落在该价位的成交额（元）。

    停牌/缺数（amount 为 0 或 NaN）的行不进分布，否则会给休盘整段造出一个假峰。
    """
    df = _usable(bars.tail(window))
    if df.empty:
        return pd.Series(dtype=float)
    lo, hi = float(df["low"].min()), float(df["high"].max())
    if hi - lo <= 0:                               # 整段一字：全部成本堆在同一价位
        return pd.Series([float(df["amount"].sum())], index=[lo])
    edges = np.linspace(lo, hi, bins + 1)
    weights = np.zeros(bins)
    lows = df["low"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    amounts = df["amount"].to_numpy(dtype=float)
    spans = np.maximum(highs - lows, 1e-9)         # 十字星没有宽度，整笔量记进它命中的那一桶
    for k in range(bins):
        overlap = np.clip(np.minimum(highs, edges[k + 1]) - np.maximum(lows, edges[k]), 0, None)
        weights[k] += float((amounts * overlap / spans).sum())
    centers = (edges[:-1] + edges[1:]) / 2.0
    return pd.Series(weights, index=centers)


def chip_peak_band(profile: pd.Series, ratio: float = PEAK_RATIO) -> tuple[float, float]:
    """主峰 = 权重最大的桶，向两侧连续扩展到「桶权重 < 峰桶 × ratio」为止，返回（下沿, 上沿）。"""
    if profile is None or profile.empty:
        raise ValueError("空的成本分布没有主峰")
    w = profile.to_numpy(dtype=float)
    peak = int(np.argmax(w))
    lo_i, hi_i = peak, peak
    while lo_i - 1 >= 0 and w[lo_i - 1] >= w[peak] * ratio:
        lo_i -= 1
    while hi_i + 1 < len(w) and w[hi_i + 1] >= w[peak] * ratio:
        hi_i += 1
    width = _bucket_width(profile)
    return (float(profile.index[lo_i]) - width / 2.0,
            float(profile.index[hi_i]) + width / 2.0)


def _bucket_width(profile: pd.Series) -> float:
    if len(profile) < 2:
        return 0.0
    return float(np.median(np.diff(profile.index.to_numpy(dtype=float))))


def atr14(bars: pd.DataFrame, window: int = ATR_WINDOW) -> float:
    """真实波幅均值：TR = max(high−low, |high−前收|, |low−前收|)，最后一根用 close 当假想前收。"""
    df = _usable(bars.tail(window + 1))
    if df.empty:
        return float("nan")
    close = df["close"].to_numpy(dtype=float)
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    prev = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev), np.abs(low - prev)))
    tr[0] = high[0] - low[0]
    return float(tr[-window:].mean())


def price_levels(bars: pd.DataFrame, window: int = WINDOW, bins: int = BINS,
                 stop_atr: float = STOP_ATR) -> dict | None:
    """三档价位 + 主峰位置读数。历史不足 window 根、分布为空或算出任一 NaN ⇒ 返回 None（不编价）。"""
    df = _usable(bars.tail(window))
    if len(df) < window:
        return None
    profile = cost_profile(bars, window=window, bins=bins)
    if profile.empty or float(profile.sum()) <= 0:
        return None
    low_edge, high_edge = chip_peak_band(profile)
    vol = atr14(bars, window=ATR_WINDOW)
    close = float(df["close"].iloc[-1])
    if not np.isfinite([low_edge, high_edge, vol, close]).all() or vol <= 0:
        return None
    buy = round(low_edge, 2)
    target = round(high_edge, 2)
    stop = round(buy - stop_atr * vol, 2)
    if target <= buy or stop <= 0:
        return None
    return {
        "chip_peak_low": buy,
        "chip_peak_high": target,
        "buy_ref_price": buy,
        "sell_target_price": target,
        "buy_invalid_price": stop,
        "atr14": round(float(vol), 4),
        "close": round(close, 2),
        "close_below_peak": bool(close < buy),          # True = 主峰在现价上方（上方套牢盘）
        "peak_gap_pct": round((close / buy - 1.0) * 100.0, 2),
        "peak_weight_pct": round(_peak_share(profile), 2),
        "chip_window": window,
    }


def levels_note(levels: dict) -> str:
    """三档价位的口径句：名单页与诊断页共用同一句，措辞不许两边各写一份再漂移。

    `现价在主峰下方` 那半句是必须的。2026-10-28 实测当日 qlib 榜 30 只**全部**现价低于主峰下沿
    （中位差约 31%），只写「买入参考 58.12」会被读成今天挂得进去，而实际要等反弹进主峰才成交。
    """
    base = (f"近 {levels['chip_window']} 日筹码主峰 {levels['buy_ref_price']:.2f}"
            f"~{levels['sell_target_price']:.2f}"
            f"（这段占了 {levels['peak_weight_pct']:.0f}% 的成交额）；"
            f"买入挂下沿、止盈挂上沿、止损=买入−2×ATR({levels['atr14']:.2f})")
    if levels["close_below_peak"]:
        base += (f"；现价在主峰下方 {abs(levels['peak_gap_pct']):.1f}%，"
                 f"挂下沿要等反弹到位")
    return base + "。规则价，不是模型预测"


def _peak_share(profile: pd.Series, ratio: float = PEAK_RATIO) -> float:
    """主峰那一段占总成本的比例：给页面一句「这段价位堆了全市场 xx% 的钱」。"""
    w = profile.to_numpy(dtype=float)
    total = float(w.sum())
    if total <= 0:
        return 0.0
    lo, hi = chip_peak_band(profile, ratio=ratio)
    inside = profile[(profile.index >= lo) & (profile.index <= hi)].to_numpy(dtype=float).sum()
    return float(inside) / total * 100.0
