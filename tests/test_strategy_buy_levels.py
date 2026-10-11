"""买点结构位：策略把判据里已经算出来的价位提成 extra，供后端落库、App 展示。

断言口径是"这两个数就是判据里的原始量"（均线 / 箱体上沿 / 6 日低点），
不是新的预测价——所以期望值全部用同一套公式在测试里重算一遍。
"""

from __future__ import annotations

import pandas as pd

from stock_selector.strategies.box_oscillate import BoxOscillate
from stock_selector.strategies.trend_rebound import TrendRebound


def frame(rows) -> pd.DataFrame:
    """rows: [(close, high, low, volume), ...]，日期按工作日补齐。"""
    df = pd.DataFrame(rows, columns=["close", "high", "low", "volume"])
    df["open"] = df["close"]
    df["date"] = pd.date_range("2024-01-01", periods=len(df), freq="B")
    return df.reset_index(drop=True)


class TestTrendReboundLevels:
    closes = [round(8 + 0.04 * i, 4) for i in range(40)]

    def hit(self):
        df = frame([(c, c + 0.05, c - 0.10, 1000.0) for c in self.closes])
        return TrendRebound({}).signal_at(df)

    def test_levels_are_the_moving_average_and_the_pullback_floor(self):
        hit = self.hit()
        assert hit is not None
        ma = sum(self.closes[-20:]) / 20            # 判据里的 MA20
        assert hit.extra["buy_ref_price"] == round(ma, 2)
        assert hit.extra["buy_invalid_price"] == round(ma * 0.95, 2)   # pullback_pct 默认 0.05

    def test_levels_survive_to_dict(self):
        assert "buy_ref_price" in self.hit().to_dict()["extra"]


class TestBoxOscillateLevels:
    def hit(self):
        box = [(10.5, 11.0, 10.0, 100.0)] * 39      # 箱体上下沿 11.0 / 10.0
        df = frame(box + [(11.3, 11.4, 10.4, 1000.0)])   # 放量突破 11*1.02=11.22
        return BoxOscillate({}).signal_at(df)

    def test_levels_are_the_breakout_line_and_the_box_roof(self):
        hit = self.hit()
        assert hit is not None, "夹具没触发箱体突破，测不到买点位"
        assert hit.extra["buy_ref_price"] == 11.22        # upper * (1 + breakout_pct)
        assert hit.extra["buy_invalid_price"] == 11.0     # 跌回箱体内即突破失败


class TestPatternReversalLevels:
    """夹具直接复用低位反转的用例（同一套列生成顺序，不另写一份容易走样的）。"""

    def frame(self):
        from test_strategy_pattern_reversal import BOTTOM, build

        return build(BOTTOM)

    def test_invalidation_is_the_six_day_low_and_there_is_no_faked_ref_price(self):
        from test_strategy_pattern_reversal import strategy

        df = self.frame()
        hit = strategy().signal_at(df)
        assert hit is not None, "夹具没命中低位反转"
        assert hit.extra["buy_ref_price"] is None         # 买点就是信号收盘价，不编第二个数
        assert hit.extra["buy_invalid_price"] == round(float(df["low"].tail(6).min()), 2)
