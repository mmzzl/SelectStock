"""rule_trigger：把 trading_rules 的价格项反算成触发线。

这里的核心断言只有一条：**线和判定必须同源**。判定用 RuleEvaluator，线是在同一条
表达式上扫 close 轴找翻转点，所以不存在"理由是跌破均线、给的价却对不上"。
"""

from __future__ import annotations

from stock_selector.backtest.rule_eval import RuleEvaluator
from stock_selector.backtest.rule_trigger import nearest_price_trigger

SELL = "price <= ma10 * 0.95"
_ev = RuleEvaluator()


class TestNearestPriceTrigger:
    def row(self, close=10.0, ma10=10.0, rsi14=40.0):
        return {"close": close, "ma10": ma10, "rsi14": rsi14}

    def test_threshold_comes_from_the_rule_itself(self):
        # 0.95 只写在表达式里，函数里没有这个常量
        assert nearest_price_trigger(SELL, self.row()) == (9.5, "down")

    def test_different_multiplier_gives_different_line(self):
        assert nearest_price_trigger("price <= ma10 * 0.8", self.row())[0] == 8.0

    def test_already_triggered_reports_boundary_above_price(self):
        # 9.0 已经跌破 9.5：最近的翻转点在上方，越过它就不再是卖出状态
        assert nearest_price_trigger(SELL, self.row(close=9.0)) == (9.5, "up")

    def test_price_alias_row_moves_together_with_close(self):
        # row 里同时带 price/close 时两个键要一起改，否则别名会盖住新值
        row = {"price": 10.0, "close": 10.0, "ma10": 10.0}
        assert nearest_price_trigger(SELL, row) == (9.5, "down")

    def test_add_rule_line_is_below_current_price(self):
        assert nearest_price_trigger("close >= ma5 * 1.03", {"close": 25.0, "ma5": 20.0}) \
            == (20.6, "down")

    def test_two_sided_rule_picks_the_nearest_boundary(self):
        row = self.row()
        got = nearest_price_trigger("price <= ma10 * 0.9 or price >= ma10 * 1.3", row)
        assert got == (9.0, "down")

    def test_rule_without_price_term_returns_none(self):
        assert nearest_price_trigger("rsi14 <= 65", self.row()) is None

    def test_blocked_conjunction_returns_none(self):
        # 另一半恒假时，价格怎么动都不会触发——这时候不能编一个价出来
        assert nearest_price_trigger("price <= ma10 * 0.95 and rsi14 >= 90",
                                     self.row()) is None

    def test_bad_inputs_return_none(self):
        for rule, row in (
            ("", self.row()), (None, self.row()), (SELL, {}), (SELL, {"close": 0}),
            (SELL, {"close": float("nan")}), (SELL, None),
        ):
            assert nearest_price_trigger(rule, row) is None

    def test_line_agrees_with_the_evaluator_on_both_sides(self):
        """线和判定同口径的直接证明：越过给到的价，eval 的结果必须翻转。"""
        for rule, row in (
            (SELL, self.row()),
            (SELL, self.row(close=9.0)),
            ("close >= ma5 * 1.03", {"close": 25.0, "ma5": 20.0}),
            ("price <= ma20", {"close": 10.0, "ma20": 7.7}),
        ):
            price, _ = nearest_price_trigger(rule, row)
            below = _ev.eval(rule, {**row, "close": price * 0.98, "price": price * 0.98})
            above = _ev.eval(rule, {**row, "close": price * 1.02, "price": price * 1.02})
            assert below != above, (rule, row, price)

    def test_precision_is_within_one_cent(self):
        row = self.row(close=13.37, ma10=12.91)
        price, _ = nearest_price_trigger(SELL, row)
        assert abs(price - 12.91 * 0.95) <= 0.01
