"""组合层现金调度（批次 E 的唯一改动）：只决定「买入/加仓今天能动多少现金」。

预注册口径见 `stock_selector_api/README.md` §6 批次 E 的 E2/E3。这里只实现那两条：

- 普通交易日留 `reserve_pct × 当日总资产` 的现金不许动（下限只卡买入与加仓，
  **不因为预留现金而强制卖任何东西**）；
- 暴跌日（全市场中位跌幅 ≤ `CRASH_MED_PCT` 且当日有行情的股票 ≥ `CRASH_MIN_STOCKS`）
  作为锚点，从锚点当天起的连续 `tranches` 个交易日里下限逐日放松 `L/tranches`，
  第 `tranches` 天完全放开；锚点过完就结束，未用完的额度不滚存；多个锚点重叠取最深那条，不叠加。

`reserve_pct = 0` 时 `floor()` 恒为 0，与现引擎行为逐条相同——这就是批次 E 的对照基准。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

# 触发口径（定死，不许事后调）：单位是百分点，-3.0 = 当日全市场中位跌幅 3%
CRASH_MED_PCT = -3.0
CRASH_MIN_STOCKS = 1500      # 2017-03 之前票池只有几十~1400 只，薄样本的"中位数"不是全市场
MARKET_STATE = Path(__file__).resolve().parent.parent.parent / "data" / "actions" / "market_state.parquet"


def crash_dates(path: Path = MARKET_STATE) -> tuple[set[str], list[str]]:
    """触发日集合 + 按日序排好的日期数组（第几天就按这个数组数）。

    市场状态文件目前覆盖 2015-06→2021-12（批次 E 的窗在内）。真要跑 2022+ 的封存段确认（E8 ③），
    得先用 `scripts/build_cross_section.py` 把这段补进文件，否则 2022 以后一个触发日都不会有。
    """
    table = pd.read_parquet(path, columns=["date", "mkt_med_pct", "mkt_n"])
    dates = pd.to_datetime(table["date"]).dt.strftime("%Y-%m-%d")
    hit = (table["mkt_med_pct"] <= CRASH_MED_PCT) & (table["mkt_n"] >= CRASH_MIN_STOCKS)
    ordered = sorted(dates.tolist())
    return set(dates[hit].tolist()), ordered


@dataclass
class CashPolicy:
    """引擎每天调一次 `floor(date, total_value)`，拿到当日不许动的现金下限。"""

    reserve_pct: float
    tranches: int
    triggers: set[str]
    days: list[str]
    log: dict[str, dict[str, float]] = field(default_factory=dict)

    @classmethod
    def build(cls, reserve_pct: float, tranches: int, path: Path = MARKET_STATE) -> "CashPolicy":
        triggers, days = crash_dates(path)
        return cls(reserve_pct=float(reserve_pct), tranches=int(tranches),
                   triggers=triggers, days=days)

    def __post_init__(self) -> None:
        self._pos = {d: i for i, d in enumerate(self.days)}

    def _released_fraction(self, date: str) -> float:
        """今天允许动用下限的多大比例：取所有仍在日程里的锚点中最深的那条。"""
        if self.reserve_pct <= 0 or not self.triggers:
            return 0.0
        today = self._pos.get(date)
        if today is None:
            return 0.0
        best = 0.0
        # 日程只持续 tranches 个交易日，所以锚点最多往前看 tranches 天
        for back in range(self.tranches):
            index = today - back
            if index < 0 or self.days[index] not in self.triggers:
                continue
            k = back + 1                     # k=1 就是锚点当天（尾盘放第一批）
            best = max(best, min(k, self.tranches) / self.tranches)
        return best

    def floor(self, date: str, total_value: float) -> float:
        base = self.reserve_pct * float(total_value)
        fraction = self._released_fraction(date)
        floor = base * (1.0 - fraction)
        self.log[date] = {"base": base, "floor": floor, "released": fraction}
        return floor

    # ---- 供批次脚本读的量 ----
    @property
    def active_days(self) -> list[str]:
        """放走过子弹（下限比基准低）的交易日——E6 要求这个数 ≥ 10 才允许读机制。"""
        return [d for d, row in self.log.items() if row["released"] > 0]
