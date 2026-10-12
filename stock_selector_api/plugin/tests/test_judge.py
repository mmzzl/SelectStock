"""test_judge.py"""
from portfolio.judge import judge_holding, sell_trigger_price
from portfolio.rules import DEFAULT_SELL_RULE

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


def test_sell_trigger_price_is_the_rule_line():
    # ma10=11 → 线在 10.45；0.95 只写在规则串里，函数没抄常量
    assert sell_trigger_price(ROW, DEFAULT_SELL_RULE) == 10.45


def test_sell_trigger_price_follows_a_custom_rule():
    assert sell_trigger_price(ROW, "price <= ma10 * 0.8") == 8.8


def test_sell_trigger_price_none_when_price_alone_cannot_flip():
    assert sell_trigger_price(ROW, "rsi14 >= 90") is None


def test_disaster_line_overrides_a_clean_trend():
    # 形态完好（不破均线）但成本价已亏 10%：判据里唯一看成本的一条
    row = dict(ROW, close=10.5, price=10.5, ma10=11.0, ma5=10.5)
    action, reason = judge_holding(row, "price <= ma10 * 0.95", "close >= ma5 * 1.03",
                                   cost_price=11.7, price=10.5, disaster_stop_pct=8.0)
    assert action == "sell"
    assert "灾难线" in reason


def test_disaster_line_ignored_inside_the_band():
    row = dict(ROW, close=10.5, price=10.5, ma10=11.0, ma5=10.5)
    action, reason = judge_holding(row, "price <= ma10 * 0.95", "close >= ma5 * 1.03",
                                   cost_price=11.0, price=10.5, disaster_stop_pct=8.0)
    assert action == "hold"


def test_disaster_line_disabled_by_default():
    row = dict(ROW, close=10.5, price=10.5, ma10=11.0, ma5=10.5)
    action, _ = judge_holding(row, "price <= ma10 * 0.95", "close >= ma5 * 1.03",
                              cost_price=100.0, price=10.5)
    assert action == "hold"


def test_disaster_line_needs_a_cost_basis():
    row = dict(ROW, close=10.5, price=10.5, ma10=11.0, ma5=10.5)
    action, _ = judge_holding(row, "price <= ma10 * 0.95", "", cost_price=0.0,
                              price=10.5, disaster_stop_pct=8.0)
    assert action == "hold"


def test_disaster_uses_live_price_not_the_parquet_close():
    # parquet 最后一根还在昨天 10.5（未破线），实时价已经砸到 9.0
    row = dict(ROW, close=10.5, price=10.5, ma10=11.0, ma5=10.5)
    action, _ = judge_holding(row, "price <= ma10 * 0.95", "", cost_price=10.0,
                              price=9.0, disaster_stop_pct=8.0)
    assert action == "sell"
    action2, _ = judge_holding(row, "price <= ma10 * 0.95", "", cost_price=10.0,
                               disaster_stop_pct=8.0)
    assert action2 == "hold"
