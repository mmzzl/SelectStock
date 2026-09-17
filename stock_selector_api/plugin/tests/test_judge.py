"""test_judge.py"""
from portfolio.judge import judge_holding

ROW = {"close": 10.0, "price": 10.0, "ma10": 11.0, "ma5": 9.5,
       "ma20": 9.0, "rsi14": 40.0, "vol_ma5": 10.0, "volume": 9.0}


def test_sell_when_below_ma10_95():
    row = dict(ROW, price=10.0, ma10=11.0)   # 10 <= 10.45 → True
    action, reason = judge_holding(row, "price <= ma10 * 0.95", "")
    assert action == "sell" and reason


def test_add_when_above_ma5_and_rsi_ok():
    row = dict(ROW, close=11.0, ma5=10.0, rsi14=50.0)
    action, reason = judge_holding(row, "close < 0", "close >= ma5 * 1.03 and rsi14 <= 65")
    assert action == "add"


def test_hold_otherwise():
    row = dict(ROW, close=10.5, price=10.5, ma10=11.0, ma5=10.5)   # 不破均线且未站上 ma5*1.03
    action, reason = judge_holding(row, "price <= ma10 * 0.95", "close >= ma5 * 1.03")
    assert action == "hold"
