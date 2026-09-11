"""箱体震荡策略插件。

逻辑：统计一段时间的高低区间构成箱体，股价有效突破箱体上沿
视为箱体突破买点，输出信号。
"""

import pandas as pd

from ..core.base_strategy import BaseSelectStrategy
from ..models.result import SelectResult


class BoxOscillate(BaseSelectStrategy):
    """箱体震荡策略：区间震荡后的突破买点。"""

    @property
    def description(self) -> str:
        return "箱体震荡：区间突破上沿买入"

    def run(self, df) -> list[SelectResult]:
        period = int(self.config.get("period", 30))
        breakout = float(self.config.get("breakout_pct", 0.02))

        df = df.sort_values("date").reset_index(drop=True)
        if len(df) < period + 1:
            return []

        box = df.iloc[-period - 1 : -1]          # 箱体统计区间（不含最新日）
        upper = float(box["high"].max())
        lower = float(box["low"].min())
        latest_close = float(df["close"].iloc[-1])

        # 1. 股价位于箱体上沿附近（未远离，说明长期区间震荡）
        prev_close = float(df["close"].iloc[-2])
        if prev_close > upper * (1 + breakout):
            return []

        # 2. 最新收盘有效突破箱体上沿（放量）
        if latest_close <= upper * (1 + breakout):
            return []

        volumes = df["volume"].iloc[-5:]
        volume_up = float(volumes.mean()) > float(df["volume"].iloc[-period - 5 : -5].mean()) * 1.2

        return [
            SelectResult(
                code="",
                name="",
                signal="box_oscillate",
                reason=(
                    f"区间 [{lower:.2f}, {upper:.2f}] 内震荡后"
                    f"收盘 {latest_close:.2f} 突破上沿"
                    + ("，量能放大" if volume_up else "")
                ),
            )
        ]