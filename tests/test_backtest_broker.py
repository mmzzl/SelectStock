"""Broker simulation account unit tests (TDD)."""

import pytest


def make_broker(**kw):
    from stock_selector.backtest.broker import Broker

    return Broker(**kw)


class TestBuy:
    def test_buy_initial_position_accumulates_cash_and_fee(self):
        broker = make_broker(capital=100000, commission_rate=0.00025,
                             commission_min=5.0, position_cap_pct=1.0)
        shares = broker.buy("600000", "Bank", "2026-09-03", price=10.0, amount=50000)
        assert shares == 5000
        commission = max(50000 * 0.00025, 5.0)  # 12.5
        assert broker.cash == pytest.approx(100000 - 50000 - commission)
        assert broker.positions["600000"].shares == 5000
        assert broker.positions["600000"].avg_price == pytest.approx(10.0)

    def test_buy_rounds_to_100_lot(self):
        broker = make_broker(capital=100000)
        # 3330/10 = 333 shares -> floor to 100-lot = 300
        shares = broker.buy("600000", "x", "2026-09-03", price=10.0, amount=3330)
        assert shares == 300

    def test_buy_rejected_when_insufficient_cash(self):
        broker = make_broker(capital=1000)
        with pytest.raises(ValueError):
            broker.buy("600000", "x", "2026-09-03", price=10.0, amount=50000)

    def test_buy_rejected_below_one_lot(self):
        broker = make_broker(capital=100000)
        with pytest.raises(ValueError):
            broker.buy("600000", "x", "2026-09-03", price=10.0, amount=99)

    def test_buy_respects_max_positions(self):
        broker = make_broker(capital=100000, max_positions=1)
        broker.buy("600000", "x", "d", price=10.0, amount=10000)
        with pytest.raises(ValueError):
            broker.buy("600001", "y", "d", price=10.0, amount=10000)

    def test_buy_respects_single_stock_cap(self):
        broker = make_broker(capital=100000, position_cap_pct=0.2)
        with pytest.raises(ValueError):
            broker.buy("600000", "x", "d", price=10.0, amount=50000)


class TestSell:
    def test_sell_frees_cash_to_settling_t1(self):
        broker = make_broker(capital=100000, commission_rate=0.00025,
                             commission_min=5.0, stamp_tax_rate=0.0005)
        broker.buy("600000", "x", "d", price=10.0, amount=20000)  # 2000 shares
        proceeds = broker.sell("600000", "d2", price=12.0)
        gross = 2000 * 12.0  # 24000
        fee = max(gross * 0.00025, 5.0) + gross * 0.0005
        assert proceeds == pytest.approx(gross - fee)
        assert broker.settling == pytest.approx(gross - fee)
        assert 600000 not in broker.positions

    def test_t1_settle_moves_to_cash_next_day(self):
        broker = make_broker(capital=100000, commission_min=5.0, commission_rate=0.00025,
                             stamp_tax_rate=0.0005)
        broker.buy("600000", "x", "d", price=10.0, amount=20000)
        broker.sell("600000", "d2", price=12.0)
        proceeds = broker.settling
        cash_before = broker.cash
        broker.settle()
        assert broker.settling == 0
        assert broker.cash == pytest.approx(cash_before + proceeds)

    def test_sell_partial_shares(self):
        broker = make_broker(capital=100000)
        broker.buy("600000", "x", "d", price=10.0, amount=20000)  # 2000 shares
        broker.sell("600000", "d2", price=12.0, shares=500)
        assert broker.positions["600000"].shares == 1500

    def test_sell_rejected_when_no_position(self):
        broker = make_broker(capital=100000)
        with pytest.raises(KeyError):
            broker.sell("600000", "d", price=12.0)


class TestAdd:
    def test_add_updates_avg_price(self):
        broker = make_broker(capital=100000, position_cap_pct=1.0)
        broker.buy("600000", "x", "d", price=10.0, amount=10000)  # 1000 @ 10
        broker.add("600000", "x", "d2", price=20.0, amount=20000)  # 1000 @ 20
        avg = (1000 * 10 + 1000 * 20) / 2000
        assert broker.positions["600000"].shares == 2000
        assert broker.positions["600000"].avg_price == pytest.approx(avg)

    def test_add_does_not_count_against_max_positions(self):
        broker = make_broker(capital=100000, max_positions=1, position_cap_pct=1.0)
        broker.buy("600000", "x", "d", price=10.0, amount=10000)
        broker.add("600000", "x", "d2", price=10.0, amount=10000)
        assert broker.positions["600000"].shares == 2000


class TestTotalValue:
    def test_total_value_sums_cash_and_positions(self):
        broker = make_broker(capital=100000)
        broker.buy("600000", "x", "d", price=10.0, amount=10000)  # 1000 shares
        tv = broker.total_value({"600000": 12.0})
        assert tv == pytest.approx(broker.cash + 1000 * 12.0)
