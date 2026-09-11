"""回测量化指标：收益/年化/回撤/胜率/夏普/盈亏比。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class Metrics:
    equity_curve: list[float] = field(default_factory=list)
    initial_capital: float = 100000.0
    closed_pnls: list[float] = field(default_factory=list)
    max_positions_used: int = 0

    @property
    def total_return(self) -> float:
        if not self.equity_curve:
            return 0.0
        return self.equity_curve[-1] / self.initial_capital - 1

    @property
    def annual_return(self) -> float:
        n = len(self.equity_curve)
        if n < 2:
            return 0.0
        intervals = n - 1
        ratio = self.equity_curve[-1] / self.initial_capital
        return ratio ** (245 / intervals) - 1

    @property
    def max_drawdown(self) -> float:
        peak = 0.0
        max_dd = 0.0
        for v in self.equity_curve:
            if v > peak:
                peak = v
            if peak > 0:
                dd = (v - peak) / peak
                if dd < max_dd:
                    max_dd = dd
        return max_dd

    @property
    def sharpe(self) -> float:
        if len(self.equity_curve) < 2:
            return 0.0
        rets = [b / a - 1 for a, b in zip(self.equity_curve, self.equity_curve[1:])]
        if not rets:
            return 0.0
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / len(rets)
        std = math.sqrt(var)
        if std == 0:
            return 0.0
        return mean / std * math.sqrt(245)

    @property
    def win_rate(self) -> float:
        n = len(self.closed_pnls)
        if n == 0:
            return 0.0
        wins = sum(1 for p in self.closed_pnls if p > 0)
        return wins / n

    @property
    def profit_factor(self) -> float:
        gross_profit = sum(p for p in self.closed_pnls if p > 0)
        gross_loss = abs(sum(p for p in self.closed_pnls if p < 0))
        if gross_loss == 0:
            return math.inf if gross_profit > 0 else 0.0
        return gross_profit / gross_loss

    @property
    def trade_count(self) -> int:
        return len(self.closed_pnls)

    def to_dict(self) -> dict:
        return {
            "total_return": round(self.total_return, 6),
            "annual_return": round(self.annual_return, 6),
            "max_drawdown": round(self.max_drawdown, 6),
            "win_rate": round(self.win_rate, 6),
            "sharpe": round(self.sharpe, 6),
            "profit_factor": self._pf_serializable(self.profit_factor),
            "trade_count": self.trade_count,
        }

    @staticmethod
    def _pf_serializable(v: float):
        if v == math.inf:
            return None
        return round(v, 6)
