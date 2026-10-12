"""intraday/indicators.py — 分钟线指标：参数与日线宽表一字不差。

参数抄自 `scripts/build_kline_features.py` 的 `INDICATOR_FUNCS`（SMA 5/10/20/60、
MACD 12/26/9、RSI 14、BBANDS 20/2/2）。这样「日线金叉」和「分钟金叉」是同一个定义在
不同周期上的投影，不会出现两套口径互相打脸，复盘时也能直接对照日线宽表。

不足 `MIN_BARS` 根一律返回 None：均线要 60 根，用 7 根凑出来的 ma20 是个假数，
拿着它判金叉会天天报警。宁可不给指标，也不给半温的指标。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import talib

MIN_BARS = 60                     # ma60 要求 60 根，不足连最后一个值都是 NaN
# 指标名与日线宽表同名（ma5/ma20/macd_dif/rsi14/boll_upper…），复盘时两边可直接对照


@dataclass
class Snapshot:
    """最后一根收口条的指标 + 上一根的指标：交叉判定要的就是这两列。"""

    bars: int
    minute: int
    current: dict
    previous: dict

    @property
    def price(self) -> float | None:
        return self.current.get("close")


def _series(bars: list[dict], column: str) -> np.ndarray:
    return np.asarray([float(b.get(column) or 0.0) for b in bars], dtype=float)


def _value(arr, index: int) -> float | None:
    """取序列第 index 个值；越界或 NaN 给 None（redis 与 parquet 都不存 NaN）。"""
    try:
        value = arr[index]
    except IndexError:
        return None
    if value is None or pd.isna(value):
        return None
    return round(float(value), 6)


def compute(bars: list[dict], min_bars: int = MIN_BARS) -> Snapshot | None:
    """对**已收口**的分钟条算指标；条数不足或列不齐返回 None。"""
    if len(bars) < min_bars:
        return None
    close = _series(bars, "close")
    volume = _series(bars, "volume")

    dif, dea, hist = talib.MACD(close, fastperiod=12, slowperiod=26, signalperiod=9)
    upper, mid, lower = talib.BBANDS(close, timeperiod=20, nbdevup=2, nbdevdn=2, matype=0)
    arrays = {
        "ma5": talib.SMA(close, timeperiod=5),
        "ma10": talib.SMA(close, timeperiod=10),
        "ma20": talib.SMA(close, timeperiod=20),
        "ma60": talib.SMA(close, timeperiod=60),
        "macd_dif": dif, "macd_dea": dea, "macd_hist": hist,
        "rsi14": talib.RSI(close, timeperiod=14),
        "boll_upper": upper, "boll_mid": mid, "boll_lower": lower,
        "vol_ma5": talib.SMA(volume, timeperiod=5),
    }
    last, prev = len(bars) - 1, len(bars) - 2
    current = {key: _value(arr, last) for key, arr in arrays.items()}
    previous = {key: _value(arr, prev) for key, arr in arrays.items()}
    for name, values in (("close", close), ("volume", volume)):
        current[name] = _value(values, last)
        previous[name] = _value(values, prev)
    current["bar_pct"] = _pct(close[last], close[prev])
    previous["bar_pct"] = _pct(close[prev], close[prev - 1]) if prev >= 1 else None
    return Snapshot(bars=len(bars), minute=int(bars[last]["minute"]),
                    current=current, previous=previous)


def _pct(now: float, before: float) -> float | None:
    """相邻两根的涨跌幅（百分数，与日线 `pct` 同一读法）。分母为 0 时给 None。"""
    if not before:
        return None
    return round((now / before - 1) * 100, 4)


def crossed_up(snap: Snapshot, key: str, ref: str) -> bool:
    """`key` 上穿 `ref`：上一根还在下，这一根到了上面。两边任一个缺数都不算交叉。"""
    cur, prev = snap.current.get(key), snap.previous.get(key)
    ref_cur, ref_prev = snap.current.get(ref), snap.previous.get(ref)
    if None in (cur, prev, ref_cur, ref_prev):
        return False
    return prev <= ref_prev and cur > ref_cur


def crossed_down(snap: Snapshot, key: str, ref: str) -> bool:
    return crossed_up(snap, ref, key)


def level_up(snap: Snapshot, key: str = "close", ref: str = "boll_upper") -> bool:
    """价格上穿某条线（突破上轨/压力位同一个写法）。"""
    return crossed_up(snap, key, ref)


def level_down(snap: Snapshot, key: str = "close", ref: str = "boll_lower") -> bool:
    return crossed_down(snap, key, ref)
