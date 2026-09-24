"""箱体震荡策略插件。

逻辑：统计一段时间的高低区间构成箱体，股价有效突破箱体上沿
视为箱体突破买点，输出信号。
"""

from ..core.base_strategy import BaseSelectStrategy
from ..models.result import SelectResult


class BoxOscillate(BaseSelectStrategy):
    """箱体震荡策略：区间震荡后的突破买点。"""

    @property
    def description(self) -> str:
        return "箱体震荡：区间突破上沿买入"

    def required_rows(self) -> int:
        return int(self.config.get("period", 30)) + 10

    def signal_at(self, df) -> SelectResult | None:
        period = int(self.config.get("period", 30))
        breakout = float(self.config.get("breakout_pct", 0.02))

        df = df.sort_values("date").reset_index(drop=True)
        if len(df) < period + 1:
            return None

        box = df.iloc[-period - 1 : -1]          # 箱体统计区间（不含最新日）
        upper = float(box["high"].max())
        lower = float(box["low"].min())
        latest_close = float(df["close"].iloc[-1])

        # 1. 股价位于箱体上沿附近（未远离，说明长期区间震荡）
        prev_close = float(df["close"].iloc[-2])
        if prev_close > upper * (1 + breakout):
            return None

        # 2. 最新收盘有效突破箱体上沿（放量）
        if latest_close <= upper * (1 + breakout):
            return None

        volumes = df["volume"].iloc[-5:]
        volume_up = float(volumes.mean()) > float(df["volume"].iloc[-period - 5 : -5].mean()) * 1.2

        # 信号强度：突破幅度越足、且放量则越高
        excess = (latest_close / upper - 1 - breakout) / breakout
        score = min(1.0, max(0.0, excess)) + (0.2 if volume_up else 0.0)

        return SelectResult(
            code="",
            name="",
            signal="box_oscillate",
            score=round(min(score, 1.0), 4),
            reason=(
                f"区间 [{lower:.2f}, {upper:.2f}] 内震荡后"
                f"收盘 {latest_close:.2f} 突破上沿"
                + ("，量能放大" if volume_up else "")
            ),
        )