"""趋势回踩策略插件。

逻辑：中期均线（MA20）呈上升趋势，股价回调触及均线后企稳，
视为趋势回踩买点，输出信号。
"""

import pandas as pd

from ..core.base_strategy import BaseSelectStrategy
from ..models.result import SelectResult


class TrendRebound(BaseSelectStrategy):
    """趋势回踩策略：上升趋势中的均线回踩买点。"""

    @property
    def description(self) -> str:
        return "趋势回踩：均线多头排列，股价回踩均线企稳"

    def run(self, df) -> list[SelectResult]:
        period = int(self.config.get("period", 20))
        pullback = float(self.config.get("pullback_pct", 0.05))

        df = df.sort_values("date").reset_index(drop=True)
        ma = df["close"].rolling(period).mean()
        if ma.isna().iloc[-1]:
            return []

        latest_close = float(df["close"].iloc[-1])
        latest_ma = float(ma.iloc[-1])
        prev_close = float(df["close"].iloc[-2]) if len(df) > 1 else latest_close

        # 1. 均线拐头向上（最近 5 日均线斜率 > 0）
        recent_ma = ma.dropna()
        slope = float(recent_ma.iloc[-1] - recent_ma.iloc[-min(5, len(recent_ma))])
        if slope <= 0:
            return []

        # 2. 股价回踩但未有效跌破均线（偏离不超过 pullback）
        deviation = (latest_close - latest_ma) / latest_ma
        if not (-pullback <= deviation <= pullback):
            return []

        # 3. 当日收阳企稳
        if latest_close < prev_close:
            return []

        return [
            SelectResult(
                code="",
                name="",
                signal="trend_rebound",
                reason=(
                    f"MA{period}={latest_ma:.2f} 多头向上，"
                    f"收盘 {latest_close:.2f} 回踩均线偏离 {deviation:.2%} 后企稳"
                ),
            )
        ]