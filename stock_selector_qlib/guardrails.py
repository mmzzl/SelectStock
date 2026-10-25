"""名单护栏：模型分数很高、但不该出现在选股名单上的两类票。定义只在这里一份。

线上打分（`serving.day_ranking`）与回测四臂（`scripts/qlib_guardrail_ab.py`）共用这两个函数，
这样「护栏把谁关在门外」这件事不会有两套口径。

两条判据都只用**当日及以前**的行情（`rolling`/`shift` 都是往回看），剪掉未来不影响历史判定，
`tests/test_qlib_guardrails.py` 里那条自求是专门钉这个的。

判不了的票（历史长度不够、那天成交额整列断供）一律**不关门**：宁可不拦，也不要在行情缺数时
把整池请出去——真缺数自有打分覆盖率告警（`serving.ranking_coverage_problem`）说出口。
"""
from __future__ import annotations

import pandas as pd

# 护栏 1 · 薄流动：近 20 个交易日日均成交额，低于**当日票池**的 1/4 分位就出局。
# 用相对分位而不是绝对金额：绝对线会随票池定义漂移（训练池 300 只的 P25 ≈ 4.1 亿，
# 全市场名单的 P25 ≈ 1.5 亿），分位这条在哪个池子说的都是同一句话「比本池四分之三的钱少」。
AMT_WINDOW = 20
AMT_MIN_DAYS = 10      # 有效天不足一半判不了均值（与 scripts/qlib_daily_universe.py 同线）
THIN_QUANTILE = 0.25

# 护栏 2 · 拉升后阴跌（妖痕）：近 SPIKE_SPAN 根 K 线里出现过「20 日涨 ≥40%」的拉升，
# 且现价距这段的高点已经回撤 ≥20%，但**最近 20 日跌幅还没超过 15%**。
# 只拦最后这一种：实测同一跌幅分桶里妖痕与安静票的后续收益没有差别（差 0.01pp、t=0.0），
# 而跌得深的那批（>25%）恰恰是模型赚钱的地方（超额 +3.24pp、t=14.4）——不能一起赶走。
SPIKE_SPAN = 71        # 找拉升、算高点回撤的窗口（交易日）
SPIKE_STEP = 20        # 拉升的度量口径：20 日涨幅
SPIKE_JUMP = 0.40
HIGH_MAX_DD = -0.20    # 距窗口内高点的回撤
R20_WINDOW = 20
R20_FLOOR = -0.15      # 20 日跌幅浅于这个数才算「还没跌深」


def thin_liquidity(amounts: pd.DataFrame) -> pd.DataFrame:
    """成交额宽表（index=交易日, columns=代码，单位元）→ 布尔宽表，True=薄流动出局。"""
    amt20 = amounts.rolling(AMT_WINDOW, min_periods=AMT_MIN_DAYS).mean()
    line = amt20.quantile(THIN_QUANTILE, axis=1)
    return amt20.lt(line, axis=0)


def spike_fade(closes: pd.DataFrame) -> pd.DataFrame:
    """收盘价宽表 → 布尔宽表，True=带妖痕且还没跌深。"""
    jumped = (closes / closes.shift(SPIKE_STEP) - 1.0).ge(SPIKE_JUMP)
    # fillna 在 astype 之前：窗口里有效天不足 71 根时 max() 是 NaN，而 bool(NaN) 会是 True
    ever = jumped.rolling(SPIKE_SPAN, min_periods=SPIKE_SPAN).max().fillna(0.0).astype(bool)
    drawdown = closes / closes.rolling(SPIKE_SPAN, min_periods=SPIKE_SPAN).max() - 1.0
    recent = closes / closes.shift(R20_WINDOW) - 1.0
    return ever & drawdown.le(HIGH_MAX_DD) & recent.gt(R20_FLOOR)


def blocked(closes: pd.DataFrame, amounts: pd.DataFrame) -> pd.DataFrame:
    """两条护栏的并集：True=当天不进名单。形状跟 `closes` 一致。"""
    yao = spike_fade(closes)
    thin = thin_liquidity(amounts).reindex(index=yao.index, columns=yao.columns, fill_value=False)
    return yao | thin
