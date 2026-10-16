"""portfolio/provider.py — 最新指标行提供者（真实=parquet；测试=内存 dict）。"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

# 需要的列：可信度模型特征 + 规则白名单里可能被用到的预处理列（含形态列）。
# lu 与 vol_z 是模型特征（见 conviction/features.py），res20/sup20 还喂 game_v/game_s 的定性代理，
# pos60/vol_ratio/bias20(close,ma20) 是核心特征，缺一样插件就不给分。
# 规则白名单（`rule_expr` 校验用的那份）里任何一列都必须在行里出现，否则管理页选得到的条件
# 线上算不出——那种缺口不会报错，只会让一条卖出规则永远「没触发」。
_INDICATOR_COLUMNS = {
    "close", "open", "high", "low", "volume", "amount",
    "ma5", "ma10", "ma20", "ma60", "ema12", "ema26",
    "macd_dif", "macd_dea", "macd_hist", "rsi14",
    "kdj_k", "kdj_d", "kdj_j", "wr14", "atr14",
    "boll_upper", "boll_mid", "boll_lower", "obv", "roc10",
    "vol_ma5", "last_close", "adx14", "pct",
    "pos60", "vol_ratio", "vol_z", "res20", "sup20", "lu",
}
# 形态/量能从 parquet 整表带过来（bool→float），不在这里再抄一份清单：patterns.PATTERN_COLUMNS
# 才是真源，抄第二次就是等着它和上游漂移。
_NON_NUMERIC = {"code", "name", "date"}


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
        # 显式列清单 + 其余全带：显式那些缺了要能被发现（插件据此不给分），
        # 其余列（形态位等）带不带到都不影响判定正确性，但规则表达式要用就得在行里。
        row = {c: float(last[c]) for c in _INDICATOR_COLUMNS if c in last.index}
        for c in df.columns:
            if c in row or c in _NON_NUMERIC:
                continue
            try:
                row[c] = float(last[c])
            except (TypeError, ValueError):
                continue
        row["code"] = str(code)
        row["name"] = str(last["name"]) if "name" in last.index else ""
        row["date"] = str(last["date"])
        return row
