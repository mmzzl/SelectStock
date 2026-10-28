"""Alpha158 + 风格/残余因子的共同定义（批次 O，2026-10-25）。

批次 N（alpha.py 的 tf_ 趋势列）结论同样约束本批：趋势当闸门是帮倒忙、当特征无显著
增量。本批从「经典量价指标 + BARRA 面板可算代理」里挑 Alpha158 **没有**的形态：

- so_rsi14      RSI(14)：涨跌日均值的不对称（If(Gt/Lt) 实现，qlib 的 Max/Min 是滚动窗口算子）
- so_macd_dif   归一化 MACD DIF（(EMA12−EMA26)/收盘）
- so_macd_hist  归一化 MACD 柱（DIF − EMA9(DIF)，再/收盘）
- so_retvol20   日**收益率** 20 日 std（Alpha158 的 STD 是价格水平 std，口径不同）
- so_skew20     日收益 20 日偏度
- so_kdj60      60 日随机指标 K：收盘在 60 日高低区间的位置
- so_amihud20   Amihud 非流动性：|日收益| 20 日均值 / 20 日成交额均值
- so_overnight  隔夜收益（今开/昨收−1）
- so_intraday   日内收益（今收/今开−1）

覆盖说明：动量（ROC1..60）、价格 std、高低差（KLEN）、量价相关 Alpha158 已有，不重复。
BARRA 里市值 Size、Beta、价值/盈利/成长/杠杆面板表达式**算不了**（Beta 要跨票对指数
回归，基本面要财报数据链），口径文档单列，别在这里找。最长窗口 60 日 = Alpha158 上
限，serving.WARMUP_DAYS=130 预热不用动。
"""
from __future__ import annotations

from qlib.contrib.data.handler import Alpha158

from .alpha import TREND_FACTORS, TREND_NAMES

_D = "Sub($close, Ref($close,1))"   # 收盘日差
_R = f"Div({_D}, Ref($close,1))"    # 日收益率
_UP = f"If(Gt({_D},0), {_D}, 0)"    # 当日上涨幅度（否则 0）
_DN = f"If(Lt({_D},0), {_D}, 0)"    # 当日下跌幅度（≤0，否则 0）

STYLE_FACTORS: list[tuple[str, str]] = [
    ("so_rsi14", f"Div(Mean({_UP},14), Sub(Mean({_UP},14), Mean({_DN},14)))"),
    ("so_macd_dif", "Div(Sub(EMA($close,12), EMA($close,26)), $close)"),
    ("so_macd_hist",
     "Div(Sub(Sub(EMA($close,12), EMA($close,26)), EMA(Sub(EMA($close,12), EMA($close,26)),9)), $close)"),
    ("so_retvol20", f"Std({_R}, 20)"),
    ("so_skew20", f"Skew({_R}, 20)"),
    ("so_kdj60", "Div(Sub($close, Min($close,60)), Sub(Max($close,60), Min($close,60)))"),
    ("so_amihud20", f"Div(Mean(Abs({_R}), 20), Mean($amount,20))"),
    ("so_overnight", "Div(Sub($open, Ref($close,1)), Ref($close,1))"),
    ("so_intraday", "Div(Sub($close, $open), $open)"),
]

# 批次 F（2026-10-28 用户拍板「涨停强信号必须通过模型排到前面」)：板后显式特征族 lu_。
# 此前所有特征都不含「当日涨幅/板旗」，模型无从知晓 002636 这类板票的存在（§40.5 实测
# 排 104 名）。板旗阈值 0.0975：主板低价票真实涨停最低 ~9.8%（p=1.02 档），0.095 会把
# 9.5%+ 的准板误旗（10-28 实测 600252 9.52% 非板）；创业板/科创板在 score_pool 源头已剔，
# 北交所 30% 板同样落旗（同属强信号，一并捕捉）。不碰标签，W_c3 配方只换特征集。
_LU_PCT = "Div(Sub($close, Ref($close,1)), Ref($close,1))"   # 当日涨幅
_LU_FLAG = f"Gt({_LU_PCT}, 0.0975)"                          # 当日涨停旗（0/1）
LU_FACTORS: list[tuple[str, str]] = [
    ("lu_pct", _LU_PCT),
    ("lu_flag", _LU_FLAG),
    ("lu_cnt5", f"Mean({_LU_FLAG}, 5)"),    # 近5日板天占比（板后热度）
    ("lu_cnt10", f"Mean({_LU_FLAG}, 10)"),  # 近10日板天占比
]
LU_NAMES = [name for name, _ in LU_FACTORS]
LU_WINDOW_MAX = 10
STYLE_NAMES = [name for name, _ in STYLE_FACTORS]
WINDOW_MAX = 60


class Alpha158Style(Alpha158):
    """Alpha158 的 158 列 + 批次 O 的 9 列；训练与打分共用这一个类。"""

    def get_feature_config(self):
        fields, names = super().get_feature_config()
        return fields + [expr for _, expr in STYLE_FACTORS], names + STYLE_NAMES


class Alpha158TrendLU(Alpha158):
    """Alpha158 + 批次 N 趋势列 + 批次 F 板后列（batchF 训练用）：W_c3 配方只换特征集。"""

    def get_feature_config(self):
        fields, names = super().get_feature_config()
        extra = [expr for _, expr in TREND_FACTORS] + [expr for _, expr in LU_FACTORS]
        return fields + extra, names + TREND_NAMES + LU_NAMES


class Alpha158Full(Alpha158):
    """Alpha158 + 批次 N + 批次 O + 批次 F 板后列（179 列）的超集。

    线上打分固定用这一个类：normalize 按 art.columns 取训练时的列，于是
    base/trend/style/full/trendlu 五代 artifact 都能从一个超集面板里合规取数。
    """

    def get_feature_config(self):
        fields, names = super().get_feature_config()
        extra = ([expr for _, expr in TREND_FACTORS] + [expr for _, expr in STYLE_FACTORS]
                 + [expr for _, expr in LU_FACTORS])
        return fields + extra, names + TREND_NAMES + STYLE_NAMES + LU_NAMES
