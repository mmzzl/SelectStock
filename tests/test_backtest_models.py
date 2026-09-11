"""Engine data structures (TDD)."""


class TestOrderAndInstruction:
    def test_order_fields(self):
        from stock_selector.backtest.models import Order

        o = Order(code="600000", amount=50000, name="Bank")
        assert o.code == "600000"
        assert o.amount == 50000
        assert o.name == "Bank"

    def test_instruction_sell_no_amount(self):
        from stock_selector.backtest.models import Instruction

        i = Instruction(side="sell", code="600000")
        assert i.side == "sell"
        assert i.amount is None


class TestEquity:
    def test_equity_fields(self):
        from stock_selector.backtest.models import EquityPoint

        e = EquityPoint(date="2026-09-03", total_value=105000.0,
                        cash=50000.0, settling=5000.0, positions_value=50000.0)
        assert e.date == "2026-09-03"
        assert e.total_value == 105000.0
        assert e.to_dict()["cash"] == 50000.0
