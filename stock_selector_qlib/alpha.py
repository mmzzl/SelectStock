"""Alpha158 + 趋势/量能扩展因子的共同定义（批次 N）。

训练（scripts/qlib_run.py）与线上打分（stock_selector_qlib/serving.py）必须用同一个类，
两边列序/定义不一致 = 「训练看这列、线上算那列」的静默漂移——所以别处不得另建 handler。

背景（2026-10-25）：MA5 趋势当作**买入闸门**已被回测证伪（换人/头名only 两版都更差），
正确用法是把同样的信息做成**训练特征**让模型自己学权重。
新增 8 列（名字前缀 tf_，与 Alpha158 的 158 列不重名）：

- tf_gap5/gap20：收盘离 5/20 日均线的距离（0 = 贴线）
- tf_ma5_slope：MA5 五日斜率（只看当日及以前，防前瞻红线内）
- tf_ma10_slope：MA10 二十日斜率
- tf_ma_spread：短均线比长均线（多头/空头排列的连续版）
- tf_hi20_dist：收盘/20 日最高（1 = 正在创新高，捕捉「买在高位」）
- tf_vol_trend / tf_amt_trend：近 5 日量/额比 20 日均值

约束：只批准 qlib 表达式（Mean/Ref/Sub/Div/Max），最长窗口 20 日——小于 Alpha158 的
60 日窗口， serving.WARMUP_DAYS=130 的预热不用动；新上市票的早期行会出 NaN，
由现有 thin 行过滤 + normalize 的 NaN→0 兜住。
"""
from __future__ import annotations

from qlib.contrib.data.handler import Alpha158

TREND_FACTORS: list[tuple[str, str]] = [
    ("tf_gap5", "Sub($close, Mean($close,5))"),
    ("tf_gap20", "Sub($close, Mean($close,20))"),
    ("tf_ma5_slope", "Div(Mean($close,5), Ref(Mean($close,5),5))"),
    ("tf_ma10_slope", "Div(Mean($close,10), Ref(Mean($close,10),20))"),
    ("tf_ma_spread", "Div(Mean($close,5), Mean($close,20))"),
    ("tf_hi20_dist", "Div($close, Max($close,20))"),
    ("tf_vol_trend", "Div(Mean($volume,5), Mean($volume,20))"),
    ("tf_amt_trend", "Div(Mean($amount,5), Mean($amount,20))"),
]
TREND_NAMES = [name for name, _ in TREND_FACTORS]
# 扩展项最长窗口（日），供预热/文档核对；不得大于 serving.WARMUP_DAYS
WINDOW_MAX = 20


class Alpha158Trend(Alpha158):
    """Alpha158 的 158 列 + 上面 8 列扩展；训练与打分共用这一个类。"""

    def get_feature_config(self):
        fields, names = super().get_feature_config()
        return fields + [expr for _, expr in TREND_FACTORS], names + TREND_NAMES
