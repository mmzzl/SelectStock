"""portfolio/provider.py — 最新指标行提供者（真实=parquet；测试=内存 dict）。"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

_INDICATOR_COLUMNS = {
    "close", "open", "high", "low", "volume", "amount",
    "ma5", "ma10", "ma20", "ma60", "ema12", "ema26",
    "macd_dif", "macd_dea", "macd_hist", "rsi14",
    "kdj_k", "kdj_d", "kdj_j", "wr14", "atr14",
    "boll_upper", "boll_mid", "boll_lower", "obv", "roc10",
    "vol_ma5", "last_close", "adx14",
    # 可信度（剧本成功率）读的列：lu 与 vol_z 是模型特征（见 conviction/features.py），
    # res20/sup20 只喂 game_v/game_s 的定性代理（见 portfolio/conviction.py）。
    # pos60/vol_ratio/bias20(close,ma20) 是核心特征，缺一样插件就不给分。
    "pos60", "vol_ratio", "vol_z", "res20", "sup20", "lu",
}


class KlineProvider:
    """从 K线 parquet 读 date ≤ natural_day 的最新一行指标（与回测同一数据面）。"""

    def __init__(self, natural_day: str, kline_dir: Path | None = None):
        self.natural_day = natural_day
        self.kline_dir = kline_dir

    def latest_row(self, code: str) -> dict | None:
        from portfolio.pricing import _code_file  # 复用文件名规则

        if self.kline_dir is None:
            p = _code_file(code)
        else:
            p = self.kline_dir / f"{code}.parquet"
        if p is None or not Path(p).is_file():
            return None
        df = pd.read_parquet(p)
        df["date"] = df["date"].astype(str).str.slice(0, 10)
        sub = df[df["date"] <= self.natural_day]
        if sub.empty:
            return None
        last = sub.sort_values("date").iloc[-1]
        row = {c: float(last[c]) for c in _INDICATOR_COLUMNS if c in last.index}
        row["code"] = str(code)
        row["name"] = str(last["name"]) if "name" in last.index else ""
        row["date"] = str(last["date"])
        return row
