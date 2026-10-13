"""入场纪律实验的口径测试：只能次日买、挂低买不到就放弃、出场规则与 broker/引擎一致。"""

import importlib.util
import sys

import numpy as np

SCRIPT = "scripts/backtest_entry_discipline.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("backtest_entry_discipline", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["backtest_entry_discipline"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load_script()

D = np.array(["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"])


def _sim(open_, low, close, signal="2026-01-05", amount=15000.0,
         stop=0.05, take=0.10, hold=15, discount=0.0):
    # 交易日历截到给定的天数， simulate_entry 以 len(dates) 为数据边界
    dates = D[:len(close)]
    return mod.simulate_entry(dates, np.array(open_, dtype="float64"),
                              np.array(low, dtype="float64"),
                              np.array(close, dtype="float64"),
                              signal, amount, stop, take, hold, discount)


def test_signal_day_itself_cannot_be_bought_only_next_trading_day():
    # 信号日次一天开盘 10 元；折价 0 一定成交在开盘价
    r = _sim([10, 10, 10], [10, 9.9, 9.9], [10, 10, 10])
    assert r is not None and r["fill"] == 10.0
    # 信号日就是样本最后一天：没有次日可买
    assert _sim([10, 10, 10], [10, 10, 10], [10, 10, 10], signal="2026-01-07") is None


def test_signal_date_missing_from_daily_returns_none():
    assert _sim([10, 10, 10], [10, 9, 9], [10, 10, 10], signal="2026-01-04") is None


def test_t_plus_1_entry_day_cannot_be_closed_out():
    # 次日（买入当天）收盘 8 元，已在止损线 9.5 以下，但 T+1 不能卖；
    # 真正的止损发生在再下一天，held=1 而不是 0
    r = _sim([10, 10, 10], [10, 5, 9], [10, 8, 8])
    assert r["fill"] == 10.0 and r["held"] == 1 and r["reason"] == "stop"
    assert r["exit"] == 8.0


def test_limit_not_touched_means_the_trade_is_skipped():
    # 次日开盘 10、最低 9.9：挂 2% 折价（9.8）买不到
    assert _sim([10, 10], [9.9, 9.9], [10, 10], discount=0.02) is None
    # 同样这批数据挂 0.5%（9.95）能成交在 9.95
    r = _sim([10, 10], [9.9, 9.9], [10, 10], discount=0.005)
    assert abs(r["fill"] - 9.95) < 1e-9


def test_gap_down_open_better_than_limit_fills_at_open():
    # 次日直接低开在限价以下：按更低的开盘价成交，不是按限价
    r = _sim([10, 9.5, 9.5], [10, 9.4, 9.4], [10, 9.5, 9.5], discount=0.02)
    assert r["fill"] == 9.5


def test_discount_0_is_anchor_on_signal_close_and_refuses_to_chase():
    # 限价挂在昨收（折价 0）：次日跳空高开、全天不回昨收 → 这笔直接放弃，不是按开盘价买
    assert _sim([10, 10.5, 10.6], [10, 10.4, 10.5], [10, 10.6, 10.7], discount=0.0) is None
    # 同一批数据，昨收以下就能买到的日子照常成交
    r = _sim([10, 9.9, 10], [10, 9.8, 9.9], [10, 10, 10.2], discount=0.0)
    assert r["fill"] == 9.9


def test_pnl_matches_broker_convention_only_sell_fee_deducted():
    # 10 元买 1500 股(=15000)，第 2 天涨到 11 元触发止盈：
    # pnl = 11*1500 − 15000 − (佣金 5? 实际 max(5, 16500*0.00025)=4.125→5 + 印花税 8.25)
    r = _sim([10, 10, 11], [10, 9.9, 10.5], [10, 10, 11], take=0.10)
    gross_sell = 11.0 * 1500
    expect = gross_sell - 15000.0 - mod._sell_fee(gross_sell)
    assert abs(r["pnl"] - expect) < 1e-6
    assert r["reason"] == "take"


def test_lots_are_rounded_down_to_100_shares():
    # 15000 / 10.7 = 1401.8 股 → 向下取整到 1400 股（整手），止盈在 12 元那天
    r = _sim([10.7, 10.7, 10.7], [10.7, 10.6, 10.6], [10.7, 10.7, 12.0])
    assert r["fill"] == 10.7 and r["reason"] == "take"
    gross_buy = 1400 * 10.7
    gross_sell = 1400 * 12.0
    assert abs(r["pnl"] - (gross_sell - gross_buy - mod._sell_fee(gross_sell))) < 1e-6


def test_max_hold_exit_uses_trading_day_index_from_entry():
    # hold=1：买入次日（d−e=1）无论涨跌都按收盘价平仓
    r = _sim([10, 10, 10], [10, 9.9, 9.9], [10, 10.5, 10.5], hold=1)
    assert r["reason"] == "time" and r["held"] == 1 and r["exit"] == 10.5


def test_stop_checked_before_take_on_the_same_close():
    # 同一天收盘价既低于止损又高于止盈不可能同时成立，改为验证优先级：
    # 收盘 9.0 只触发止损（止盈线 11 没到）
    r = _sim([10, 10, 9], [10, 9, 8], [10, 10, 9], take=0.10)
    assert r["reason"] == "stop" and r["exit"] == 9.0


def test_unsettled_at_data_end_reported_as_limit_reason():
    # 只有 3 天数据，never 触发任何出场
    r = _sim([10, 10, 10], [10, 9.9, 9.9], [10, 10.2, 10.3], hold=15)
    assert r["reason"] == "limit" and r["exit"] == 10.3


def test_summarize_payoff_expectancy_and_open_count():
    res = {0.0: [{"pnl": 1000.0, "reason": "take"}, {"pnl": -500.0, "reason": "stop"},
                 {"pnl": 500.0, "reason": "limit"}]}
    s = mod.summarize(res, 0.0, 1)
    assert s["n"] == 3 and s["wr"] == 2 / 3
    assert abs(s["payoff"] - (750.0 / 500.0)) < 1e-9   # 均盈 (1000+500)/2、均亏 500
    assert abs(s["exp"] - (1000 - 500 + 500) / 3) < 1e-9
    # 利润因子是另一个口径：总盈/总亏 = 1500/500。与赔率分开报，因为两者在低胜率时会打架。
    assert abs(s["pf"] - 3.0) < 1e-9
    assert s["open"] == 1


def test_summarize_treats_skipped_trades_as_not_counted():
    s = mod.summarize({0.02: [{}, {}]}, 0.02, 1)
    assert s == {"n": 0, "wr": 0.0, "payoff": 0.0, "pf": 0.0, "exp": 0.0, "open": 0}
