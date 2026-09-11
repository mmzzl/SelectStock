"""trading_rules 受限求值器 + 实盘规则策略适配器测试 (TDD)."""

import pytest


class TestRuleEvaluator:
    def make(self):
        from stock_selector.backtest.rule_eval import RuleEvaluator

        return RuleEvaluator()

    def row(self):
        return {"close": 7.0, "ma10": 9.9, "adx14": 22.0, "last_close": 7.0}

    def test_eval_price_alias_to_close(self):
        ev = self.make()
        assert ev.eval("price <= ma10 * 0.8", self.row()) is True

    def test_eval_adx_alias_to_adx14(self):
        ev = self.make()
        assert ev.eval("adx <= 25 * 1.0", self.row()) is True

    def test_eval_last_close(self):
        ev = self.make()
        assert ev.eval("price < last_close * 1.0", self.row()) is False  # equal -> not <
        assert ev.eval("price <= last_close", self.row()) is True

    def test_eval_malformed_returns_false(self):
        ev = self.make()
        assert ev.eval("this is not python", self.row()) is False

    def test_eval_blocks_unsafe_builtins(self):
        ev = self.make()
        # __import__ 不可用，返回 False 而非执行
        assert ev.eval("__import__('os').getcwd() == ''", self.row()) is False


class FakeProvider:
    """包装真实的 ParquetProvider，复用 provider_dir 数据。"""

    def __init__(self, dirpath):
        from stock_selector.backtest.provider import ParquetProvider

        self._inner = ParquetProvider(dirpath)

    def rows(self, date, codes=None):
        return self._inner.rows(date, codes)

    def price(self, date, code):
        return self._inner.price(date, code)

    def prices(self, date, codes=None):
        return self._inner.prices(date, codes)


class TestRulesStrategy:
    def make(self, dirpath, buy, sell, add=None, target=20000):
        from stock_selector.backtest.rules import RulesStrategy

        prov = FakeProvider(dirpath)
        return RulesStrategy(
            provider=prov,
            buy_rule=buy,
            sell_rule=sell,
            add_rule=add,
            target_amount=target,
        )

    def test_propose_buys_returns_matching_orders(self, provider_dir):
        # 09-01: 000001 close=10.2>ma10=9.9 命中；000002 close=7.1>ma10=6.9 命中
        strat = self.make(provider_dir, buy="close > ma10", sell="close < ma10 * 0.95")
        orders = strat.propose_buys("2026-09-01", cash=100000, prices={})
        assert [o.code for o in orders] == ["000001", "000002"]
        assert all(o.amount == 20000 for o in orders)

    def test_propose_buys_skips_paused(self, provider_dir):
        # 09-02: 000002 停牌被跳过；000001 close=10.7>ma10=10.0 命中
        strat = self.make(provider_dir, buy="close > ma10", sell="close < ma10 * 0.95")
        orders = strat.propose_buys("2026-09-02", cash=100000, prices={})
        assert [o.code for o in orders] == ["000001"]

    def test_evaluate_holdings_sell_signal(self, provider_dir):
        # 09-02 000001 close=10.7, ma10=10.0; sell condition 10.7 < 10.0*0.95=9.5 -> False
        strat = self.make(provider_dir, buy="close > ma10", sell="close < ma10 * 0.95")
        instr = strat.evaluate_holdings("2026-09-02", ["000001"], prices={})
        assert instr == []

    def test_evaluate_holdings_sell_signals_on_condition(self, provider_dir):
        # 用更严的 sell 规则使其命中
        strat = self.make(provider_dir, buy="close > ma10", sell="close < ma10 * 1.1")
        instr = strat.evaluate_holdings("2026-09-02", ["000001"], prices={})
        assert len(instr) == 1
        assert instr[0].side == "sell"
        assert instr[0].code == "000001"

    def test_evaluate_holdings_add_signal(self, provider_dir):
        # sell 不触发 (10.7 > ma10*0.5)；add 触发 (10.7 > ma5=10.1)
        strat = self.make(provider_dir, buy="close > ma10", sell="close < ma10 * 0.5",
                          add="close > ma5")
        instr = strat.evaluate_holdings("2026-09-02", ["000001"], prices={})
        sides = [i.side for i in instr]
        assert "add" in sides
