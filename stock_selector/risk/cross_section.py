"""risk/cross_section.py — 当日**全市场**横截面百分位。

为什么这一层单独存在：单票类风控规则判的都是「这一只」（涨停买不进、日均成交额太薄、
单票仓位超了、滑点太大、买入风险分低），分母是它自己。而 `adx14` 闸门判的是
「这一只在**当天所有票**里排第几成」——分母是全市场 6600 只。

分母这件事必须与回测逐字一致，否则实测过的九格全正就不是实盘那一句：
回测最初用「回测股票池」当分母，1500 只的池里"前 10%"是当日 150 只，实盘是当日 660 只，
同一句规则在两处严格程度不同。改成全市场分母后重跑，九格仍全正
（见 README《结论卡：adx14 高分位闸门》），所以实盘这边只实现全市场这一种。

两个取数的选择，都是为了不再读一遍全市场：
- 内核（`core/selector.py`）在逐股遍历**之前**先做一趟窄列预扫（只读 `adx14` 与日期），
  拿到当日全市场分位，再把分位喂给每只票的 `RiskContext.cross_pct`；
- 盘后判定台账（`scripts/build_judgement_ledger.py`）本来就要逐日扫全市场，直接复用同一函数。

百分位用 pandas 的 `rank(pct=True, method="average")`：并列取平均名次，
缺失值（指标未成形、停牌）保持 NaN 而不是当最低——把缺失当最低会凭空造出一个"最差的一箱"。
"""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import pandas as pd

FACTOR = "adx14"
MIN_CROSS_SECTION = 100        # 当日分母少于这个数就不判（样本太少的百分位没有意义）


def daily_percentile(values: dict[str, float], factor: str = FACTOR) -> dict[str, float]:
    """{代码: 指标值} → {代码: 当日百分位 0~100}；值缺失/非数的票不出现在结果里。

    并列取平均名次（`method="average"`）而不是先到先得：ADX 这类指标并列很常见，
    先到先得会让同一批票因为代码顺序不同而落在不同箱里。
    """
    clean = {c: float(v) for c, v in values.items()
             if v is not None and np.isfinite(float(v))}
    if not clean:
        return {}
    series = pd.Series(clean, dtype="float64")
    return (series.rank(pct=True, method="average") * 100.0).to_dict()


def collect(frames: Iterable[tuple[str, pd.DataFrame]], factor: str = FACTOR) -> dict[str, float]:
    """从 (代码, 日线切片) 里取每只票**最后一根**的指标值。

    只取最后一根而不是全表：横截面要的是「基准日当天的状态」，拿历史值排位等于
    用过去的排名判今天。切片里没有该列（Mongo 源没有指标列）就整只跳过——
    上层会看到"没有分位"并按「信息不够就不判」放行，而不是当成最低分位。
    """
    out: dict[str, float] = {}
    for code, df in frames:
        if df is None or df.empty or factor not in df.columns:
            continue
        value = df[factor].iloc[-1]
        try:
            out[str(code)] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def enough(pct: dict[str, float] | None) -> bool:
    """当日分母够不够大。不够就整层不判（上层放行）。"""
    return bool(pct) and len(pct) >= MIN_CROSS_SECTION
