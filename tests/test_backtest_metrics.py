"""Metrics unit tests (TDD)."""

import math

import pytest


def make_metrics(equity_curve, initial, closed_pnls=None):
    from stock_selector.backtest.metrics import Metrics

    return Metrics(equity_curve=equity_curve, initial_capital=initial,
                   closed_pnls=closed_pnls or [])


class TestReturns:
    def test_total_return_simple(self):
        m = make_metrics([100000, 110000, 120000], 100000)
        assert m.total_return == pytest.approx(0.20)

    def test_annual_return_uses_245_trading_days(self):
        # 2 points, 1 trading interval; annual = (end/start)^(245/1)-1
        m = make_metrics([100000, 110000], 100000)
        expected = (110000 / 100000) ** (245 / 1) - 1
        assert m.annual_return == pytest.approx(expected, rel=1e-6)

    def test_annual_return_single_point_is_zero(self):
        m = make_metrics([100000], 100000)
        assert m.annual_return == 0.0


class TestDrawdown:
    def test_max_drawdown(self):
        # peak 110000 -> trough 90000 = -18.18%
        m = make_metrics([100000, 110000, 90000, 95000], 100000)
        assert m.max_drawdown == pytest.approx((90000 - 110000) / 110000)

    def test_no_drawdown_when_strictly_up(self):
        m = make_metrics([100000, 101000, 102000], 100000)
        assert m.max_drawdown == 0.0


class TestSharpe:
    def test_sharpe_zero_return_is_zero(self):
        m = make_metrics([100000, 100000, 100000], 100000)
        assert m.sharpe == 0.0

    def test_sharpe_formula(self):
        curve = [100000, 101000, 102000, 100500, 103000]
        m = make_metrics(curve, 100000)
        rets = [b / a - 1 for a, b in zip(curve, curve[1:])]
        mean = sum(rets) / len(rets)
        std = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets))
        expected = mean / std * math.sqrt(245) if std > 0 else 0.0
        assert m.sharpe == pytest.approx(expected, rel=1e-6)


class TestWinRate:
    def test_win_rate_closed_trades(self):
        pnls = [1000, -500, 200, -300]
        m = make_metrics([100000], 100000, closed_pnls=pnls)
        assert m.win_rate == pytest.approx(0.5)

    def test_win_rate_zero_when_no_trades(self):
        m = make_metrics([100000], 100000)
        assert m.win_rate == 0.0

    def test_win_rate_positives_only(self):
        m = make_metrics([100000], 100000, closed_pnls=[100, 200])
        assert m.win_rate == pytest.approx(1.0)


class TestProfitFactor:
    def test_profit_factor(self):
        pnls = [1000, 2000, -500, -300]
        m = make_metrics([100000], 100000, closed_pnls=pnls)
        gross_profit = 3000
        gross_loss = 800
        assert m.profit_factor == pytest.approx(gross_profit / gross_loss)

    def test_profit_factor_zero_loss_returns_inf(self):
        m = make_metrics([100000], 100000, closed_pnls=[100, 200])
        assert m.profit_factor == math.inf

    def test_profit_factor_zero_when_no_gain(self):
        m = make_metrics([100000], 100000, closed_pnls=[-100, -200])
        assert m.profit_factor == 0.0


class TestTradeCount:
    def test_trade_count_is_num_closed(self):
        m = make_metrics([100000], 100000, closed_pnls=[1, 2, 3, 4])
        assert m.trade_count == 4
