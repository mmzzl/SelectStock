"""回测量化指标：收益/年化/回撤/胜率/夏普/盈亏比。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


TRADING_DAYS_PER_YEAR = 245
# 年化只是把区间收益按 245 日幂外推：观测太少时外推出来的数完全是噪声
# （两个交易日 −0.5% 会外推成「年化 −72%」），不足这个间隔数就不给数字。
MIN_ANNUAL_INTERVALS = 20


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
    def annual_return(self) -> float | None:
        """区间收益外推成年化；样本不足返回 None，交给前端显示「样本不足」。"""
        intervals = len(self.equity_curve) - 1
        if intervals < MIN_ANNUAL_INTERVALS:
            return None
        ratio = self.equity_curve[-1] / self.initial_capital
        return ratio ** (TRADING_DAYS_PER_YEAR / intervals) - 1

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
        return mean / std * math.sqrt(TRADING_DAYS_PER_YEAR)

    @property
    def win_rate(self) -> float:
        n = len(self.closed_pnls)
        if n == 0:
            return 0.0
        wins = sum(1 for p in self.closed_pnls if p > 0)
        return wins / n

    @property
    def profit_factor(self) -> float:
        """总盈利/总亏损（钱量口径）：>1 说明赚的钱比亏的多，与单笔赔率不是一回事。"""
        gross_profit = sum(p for p in self.closed_pnls if p > 0)
        gross_loss = abs(sum(p for p in self.closed_pnls if p < 0))
        if gross_loss == 0:
            return math.inf if gross_profit > 0 else 0.0
        return gross_profit / gross_loss

    @property
    def avg_win(self) -> float:
        wins = [p for p in self.closed_pnls if p > 0]
        return sum(wins) / len(wins) if wins else 0.0

    @property
    def avg_loss(self) -> float:
        losses = [abs(p) for p in self.closed_pnls if p < 0]
        return sum(losses) / len(losses) if losses else 0.0

    @property
    def payoff_ratio(self) -> float:
        """平均盈利/平均亏损（单笔赔率口径）：止盈止损不对称就直接体现在这里。"""
        if self.avg_loss == 0:
            return math.inf if self.avg_win > 0 else 0.0
        return self.avg_win / self.avg_loss

    @property
    def expectancy(self) -> float:
        """单笔期望盈亏（元，已扣佣金/印花税）：赔率再高，这个 <=0 就是亏钱。"""
        if not self.closed_pnls:
            return 0.0
        return sum(self.closed_pnls) / len(self.closed_pnls)

    @property
    def trade_count(self) -> int:
        return len(self.closed_pnls)

    def to_dict(self) -> dict:
        annual = self.annual_return
        return {
            "total_return": round(self.total_return, 6),
            "annual_return": None if annual is None else round(annual, 6),
            "max_drawdown": round(self.max_drawdown, 6),
            "win_rate": round(self.win_rate, 6),
            "sharpe": round(self.sharpe, 6),
            "profit_factor": self._pf_serializable(self.profit_factor),
            "payoff_ratio": self._pf_serializable(self.payoff_ratio),
            "avg_win": round(self.avg_win, 2),
            "avg_loss": round(self.avg_loss, 2),
            "expectancy": round(self.expectancy, 2),
            "trade_count": self.trade_count,
        }

    @staticmethod
    def _pf_serializable(v: float):
        if v == math.inf:
            return None
        return round(v, 6)
