"""基础日线派生列：板块涨停线判定 + pct/lu/vol_ratio/pos60。"""

import numpy as np
import pandas as pd

LIMIT_MAIN = 9.8
LIMIT_GEM = 19.5
LIMIT_BJ = 29.5


def board_limit_threshold(code: str) -> float:
    """按股票代码前缀返回主板/创业板/科创板/北交所的涨停涨幅阈值(%)。"""
    if code.startswith(("300", "301", "688")):
        return LIMIT_GEM
    if code.startswith(("8", "4", "920")):
        return LIMIT_BJ
    return LIMIT_MAIN


def is_limit_up(code: str, pct: float) -> bool:
    """当且仅当涨幅 pct(%) 达到该板块涨停线时返回 True。"""
    return pct >= board_limit_threshold(code)


def add_base_columns(df: pd.DataFrame) -> pd.DataFrame:
    """在 df 上原位新增 4 个基础派生列并返回。

    依赖 df 已有列：code, close, volume, vol_ma5。
    新增：
      - pct      当日涨跌幅(%)
      - lu       板块感知涨停标记(bool)
      - vol_ratio  量比 = volume / vol_ma5
      - pos60    现价在 60 日高低区间的分位
    """
    close = df["close"].astype(float)
    pct = close.pct_change() * 100.0
    df["pct"] = pct

    thr = df["code"].map(board_limit_threshold)
    df["lu"] = pct >= thr

    vol_ma5 = df["vol_ma5"].astype(float)
    df["vol_ratio"] = df["volume"].astype(float) / vol_ma5.replace(0, np.nan)

    max60 = close.rolling(60).max()
    min60 = close.rolling(60).min()
    df["pos60"] = (close - min60) / (max60 - min60 + 1e-9)
    return df
