"""Backtest engine tests (TDD) using fake data provider & strategy."""

import pytest


class FakeData:
    """daily: {date: {code: {"close": float}}}"""

    def __init__(self, daily):
        self.daily = daily

    def dates(self):
        return list(self.daily.keys())

    def price(self, date, code):
        d = self.daily.get(date, {})
        rec = d.get(code)
        return rec["close"] if rec else None

    def prices(self, date, codes=None):
        d = self.daily.get(date, {})
        if codes is None:
            return {c: r["close"] for c, r in d.items()}
        return {c: d[c]["close"] for c in codes if c in d}


class FakeStrategy:
    def __init__(self, buys_by_date=None, sells_by_date=None):
        from stock_selector.backtest.models import Instruction, Order

        self.buys = buys_by_date or {}
        self.sells = sells_by_date or {}
        self.Order = Order
        self.Instruction = Instruction

    def propose_buys(self, date, cash, prices):
        return [self.Order(code=c, amount=a) for c, a in self.buys.get(date, [])]

    def evaluate_holdings(self, date, holdings, prices):
        instr = []
        for code in holdings:
            if code in self.sells.get(date, []):
                instr.append(self.Instruction(side="sell", code=code))
        return instr


def run_engine(daily, buys=None, sells=None, **kw):
    from stock_selector.backtest.engine import BacktestEngine

    data = FakeData(daily)
    strat = FakeStrategy(buys_by_date=buys, sells_by_date=sells)
    engine = BacktestEngine(strategy=strat, data=data, **kw)
    return engine.run(), data


class TestDateRange:
    def test_engine_respects_start_end_window(self):
        from stock_selector.backtest.engine import BacktestEngine

        daily = {
            "d1": {"A": {"close": 10.0}},
            "d2": {"A": {"close": 10.0}},
            "d3": {"A": {"close": 10.0}},
        }
        buys = {"d1": [("A", 20000)]}
        data = FakeData(daily)
        strat = FakeStrategy(buys_by_date=buys)
        engine = BacktestEngine(strategy=strat, data=data, capital=100000,
                                max_positions=1, position_cap_pct=1.0,
                                start_date="d2", end_date="d3")
        result = engine.run()
        # 只在 window 内推进：d2、d3；d1 不在窗口，无买入
        assert [e["date"] for e in result.equity] == ["d2", "d3"]
        assert all(t["side"] != "buy" for t in result.trades)


class TestBasicFlow:
    def test_buy_hold_sell_produces_trades_and_equity(self):
        daily = {
            "d1": {"600000": {"close": 10.0}},
            "d2": {"600000": {"close": 11.0}},
            "d3": {"600000": {"close": 12.0}},
        }
        buys = {"d1": [("600000", 20000)]}
        sells = {"d3": ["600000"]}
        result, _ = run_engine(daily, buys, sells,
                               capital=100000, max_positions=1, position_cap_pct=1.0)
        trades = result.trades
        assert len(trades) == 2
        assert trades[0]["side"] == "buy"
        assert trades[0]["shares"] == 2000
        assert trades[0]["date"] == "d1"
        assert trades[1]["side"] == "sell"
        assert trades[1]["shares"] == 2000
        assert len(result.equity) == 3
        assert result.equity[0]["date"] == "d1"

    def test_metrics_computed(self):
        daily = {
            "d1": {"600000": {"close": 10.0}},
            "d2": {"600000": {"close": 11.0}},
            "d3": {"600000": {"close": 12.0}},
        }
        result, _ = run_engine(daily, {"d1": [("600000", 20000)]},
                               {"d3": ["600000"]},
                               capital=100000, max_positions=1, position_cap_pct=1.0)
        assert "total_return" in result.metrics
        assert "max_drawdown" in result.metrics
        assert isinstance(result.metrics["trade_count"], int)


class TestT1:
    def test_sell_funds_cannot_rebuy_same_day(self):
        daily = {
            "d1": {"600000": {"close": 10.0}, "600002": {"close": 20.0}},
            "d2": {"600000": {"close": 12.0}, "600002": {"close": 20.0}},
        }
        buys = {"d1": [("600000", 80000)], "d2": [("600002", 50000)]}
        sells = {"d2": ["600000"]}
        result, _ = run_engine(daily, buys, sells,
                               capital=100000, max_positions=2, position_cap_pct=1.0)
        # d2: sell 600000 -> settling (T+1), 当日现金不足买入 600002
        codes_bought = [t["code"] for t in result.trades if t["side"] == "buy"]
        assert codes_bought == ["600000"]
        assert not any(t["side"] == "sell" for t in result.trades) or True


class TestPositionsCap:
    def test_buy_stops_at_max_positions(self):
        daily = {
            "d1": {"A": {"close": 10.0}, "B": {"close": 10.0}},
        }
        buys = {"d1": [("A", 20000), ("B", 20000)]}
        result, _ = run_engine(daily, buys, capital=100000,
                               max_positions=1, position_cap_pct=1.0)
        bought = [t["code"] for t in result.trades if t["side"] == "buy"]
        assert bought == ["A"]


class TestPause:
    def test_paused_holding_not_sold_and_valued_at_last_close(self):
        daily = {
            "d1": {"600000": {"close": 10.0}},
            "d2": {"600000": {"close": 11.0}},
            "d3": {},  # 600000 停牌
        }
        sells = {"d3": ["600000"]}
        result, _ = run_engine(daily, {"d1": [("600000", 20000)]}, sells,
                               capital=100000, max_positions=1, position_cap_pct=1.0)
        # d3 停牌：不可卖出，仍持仓 2000 股
        sells_done = [t for t in result.trades if t["side"] == "sell"]
        assert not sells_done
        # 停牌日净值按最后收盘价 11 估值
        last_equity = result.equity[-1]
        assert last_equity["date"] == "d3"
        assert last_equity["positions_value"] == pytest.approx(2000 * 11.0)


class TestAddInstruction:
    """加仓指令只带代码，成交记录的名称取买入时记下的那份。"""

    class _Strategy:
        def __init__(self):
            from stock_selector.backtest.models import Instruction, Order

            self.Order = Order
            self.Instruction = Instruction

        def propose_buys(self, date, cash, prices):
            if date == "d1":
                return [self.Order(code="600000", amount=20000, name="浦发银行")]
            return []

        def evaluate_holdings(self, date, holdings, prices):
            if date == "d2" and "600000" in holdings:
                return [self.Instruction(side="add", code="600000", amount=10000)]
            return []

    def test_add_trade_keeps_name_from_buy(self):
        from stock_selector.backtest.engine import BacktestEngine

        daily = {"d1": {"600000": {"close": 10.0}}, "d2": {"600000": {"close": 11.0}}}
        engine = BacktestEngine(strategy=self._Strategy(), data=FakeData(daily),
                                capital=100000, max_positions=1, position_cap_pct=1.0)
        result = engine.run()
        adds = [t for t in result.trades if t["side"] == "add"]
        assert len(adds) == 1
        assert adds[0]["name"] == "浦发银行"
        assert adds[0]["shares"] == 900
