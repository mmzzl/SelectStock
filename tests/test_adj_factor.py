"""复权因子（A，docs §48）：从**除权除息事件**推乘法因子台阶，而不是从厂商复权价反推。

为什么不走「拿复权价相除」那条路（2026-10-29 实测，§48.1 判决）：东财 `fqt=2` 的后复权价不是比例复权。
600519 在 2015-01→07 的 120 个交易日上，`hfq = 4.5680 ×(裸价 + 19.5942)` 拟合到最大残差 0.0052 元
（就是 2 位小数的舍入），而「裸价 × 常数因子」的乘法假设最大相对残差 2.701%。也就是说它的日涨幅被
系统性压缩了一成（裸价 −2.32% 它给 −2.11%），拿它当收益基准会把每天的涨跌整体打九折，拿它的
`hfq/裸价` 当因子也是一条随价格涨跌漂动的假曲线。腾讯 hfq 早已证伪（600608 给 −99.41 的负价、
000018 日间因子抖 43%）。所以唯一的准路是：拿交易所口径的**分红送配事件**，按
`除权(息)参考价 =(前收盘 + 配股价×配股比 − 每股现金红利)/(1 + 每股送转 + 每股配股)` 算台阶。

本模块只写算式本身：不吃行情、不碰 Mongo、不 import qlib。行情侧的校验（除权日裸价实际跌幅 vs
事件隐含跌幅、以及「跌超过涨跌停却查不到事件」的漏网除权）也写成纯函数，因为那才是判断日历抓全了
没有的唯一办法。
"""
import pytest

from stock_selector.adjfactor import (adjusted_bars, build_factors, exref_step,
                                      parse_events, unexplained_gaps)


def _event(code="600519", ex="2026-06-26 00:00:00", bonus_rmb=280.2423,
           bonus_ratio=None, it_ratio=None, progress="实施分配"):
    """一条东财 RPT_SHAREBONUS_DET 的原始行（字段名照接口，别自创）。"""
    return {"SECURITY_CODE": code, "EX_DIVIDEND_DATE": ex, "PRETAX_BONUS_RMB": bonus_rmb,
            "BONUS_RATIO": bonus_ratio, "IT_RATIO": it_ratio, "ASSIGN_PROGRESS": progress}


def _bars(pairs):
    return [{"date": d, "open": c, "high": c, "low": c, "close": c} for d, c in pairs]


def test_parse_events_normalizes_per_share_amounts():
    ev = parse_events([_event(bonus_rmb=280.2423)])[0]
    assert ev["ex_date"] == "2026-06-26"
    assert ev["cash"] == pytest.approx(28.02423)      # 10派280.24 ⇒ 每股 28.024
    assert ev["bonus"] == 0.0 and ev["transfer"] == 0.0
    assert ev["code"] == "600519"


def test_parse_events_keeps_bonus_and_transfer_separate():
    ev = parse_events([_event(ex="2024-06-01 00:00:00", bonus_rmb=30.0,
                              bonus_ratio=2.0, it_ratio=8.0)])[0]
    assert ev["cash"] == pytest.approx(3.0)
    assert (ev["bonus"], ev["transfer"]) == (0.2, 0.8)


def test_parse_events_drops_unexecuted_and_terminated_plans():
    rows = [_event(ex=None), _event(progress="停止实施"), _event(progress="预案"),
            _event(ex="2025-06-01 00:00:00", progress="实施分配")]
    got = parse_events(rows)
    assert [e["ex_date"] for e in got] == ["2025-06-01"]


def test_same_exdate_events_merge_into_one_step():
    """一天两笔实施分配（002043 在 2026-06-01：10派1.8 + 10派1.32，实测把整轮抓数撞挂在主键上）。

    交易所当天只出**一个**除权参考价，所以两笔要合并推一级台阶；分开算不仅台阶偏（1−c1 与 1−c2
    相乘 ≠ 1−(c1+c2)），还会在同一 (code,date) 上写出两行。
    """
    rows = [_event(code="002043", ex="2026-06-01 00:00:00", bonus_rmb=1.8),
            _event(code="002043", ex="2026-06-01 00:00:00", bonus_rmb=1.32)]
    ev = parse_events(rows)
    assert len(ev) == 1
    assert ev[0]["cash"] == pytest.approx(0.312)

    bars = _bars([("2026-05-29", 13.24), ("2026-06-01", 12.928), ("2026-06-02", 13.0)])
    steps = build_factors(bars, ev)
    assert len(steps) == 1
    assert steps[0]["step"] == pytest.approx((13.24 - 0.312) / 13.24)
    assert steps[0]["residual"] == pytest.approx(0.0, abs=1e-4)


def test_exref_step_cash_dividend_only():    # 20 元的票 10派5 ⇒ 参考价 (20−0.5)/20 = 0.975
    assert exref_step(20.0, cash=0.5) == pytest.approx(0.975)


def test_exref_step_split_only_halves_the_price():
    # 10送10 ⇒ 参考价 20/40 …即 (20−0)/(20×2) = 0.5
    assert exref_step(20.0, bonus=1.0) == pytest.approx(0.5)


def test_exref_step_mixes_cash_bonus_transfer_and_allotment():
    # 20 元、10派3转增7、配股价 8 元每 10 股配 2 股
    expect = (20.0 + 8.0 * 0.2 - 0.3) / (20.0 * (1 + 0.7 + 0.2))
    assert exref_step(20.0, cash=0.3, transfer=0.7,
                      allot_price=8.0, allot_ratio=0.2) == pytest.approx(expect)


def test_build_factors_step_is_cumulative_inverse_product():
    # 两笔各派 1 元、前收盘 21 与 20 ⇒ 因子 21/20、再乘 20/19
    bars = _bars([("2024-05-30", 21.0), ("2024-06-03", 20.0),
                  ("2024-06-04", 19.0), ("2024-06-05", 18.0)])
    events = [{"code": "000001", "ex_date": "2024-06-03", "cash": 1.0, "bonus": 0.0,
               "transfer": 0.0, "allot_price": 0.0, "allot_ratio": 0.0},
              {"code": "000001", "ex_date": "2024-06-04", "cash": 1.0, "bonus": 0.0,
               "transfer": 0.0, "allot_price": 0.0, "allot_ratio": 0.0}]
    steps = build_factors(bars, events)
    assert [s["date"] for s in steps] == ["2024-06-03", "2024-06-04"]
    assert steps[0]["prev_close"] == 21.0
    assert steps[0]["factor"] == pytest.approx(21.0 / 20.0)
    assert steps[1]["prev_close"] == 20.0
    assert steps[1]["factor"] == pytest.approx(21.0 / 19.0)


def test_build_factors_uses_last_bar_before_exdate_as_prev_close():
    # 除息前一天停牌没有行情：前收盘取到的上一根真实成交（2024-05-30），不是日历上的昨天
    bars = _bars([("2024-05-30", 20.0), ("2024-06-06", 19.0)])
    events = [{"code": "000001", "ex_date": "2024-06-06", "cash": 1.0, "bonus": 0.0,
               "transfer": 0.0, "allot_price": 0.0, "allot_ratio": 0.0}]
    steps = build_factors(bars, events)
    assert steps[0]["prev_close"] == 20.0
    assert steps[0]["residual"] == pytest.approx(0.0)   # 19 = 20×(19/20)，正好是纯除息


def test_build_factors_skips_event_that_precedes_all_bars():
    bars = _bars([("2024-06-05", 19.0)])
    events = [{"code": "000001", "ex_date": "2024-06-01", "cash": 1.0, "bonus": 0.0,
               "transfer": 0.0, "allot_price": 0.0, "allot_ratio": 0.0}]
    assert build_factors(bars, events) == []


def test_adjusted_bars_remove_the_fake_drop_but_keep_the_real_move():
    # 2024-06-03 除息 1 元：裸价 21→20（看着像 −4.76%），当天真实涨跌是 0
    bars = _bars([("2024-05-30", 21.0), ("2024-06-03", 20.0), ("2024-06-04", 20.5)])
    events = [{"code": "000001", "ex_date": "2024-06-03", "cash": 1.0, "bonus": 0.0,
               "transfer": 0.0, "allot_price": 0.0, "allot_ratio": 0.0}]
    adj = adjusted_bars(bars, build_factors(bars, events))
    assert [b["factor"] for b in adj] == [1.0, 21 / 20, 21 / 20]
    assert adj[1]["close"] == pytest.approx(21.0)               # 假跌没了
    assert adj[2]["close"] / adj[1]["close"] - 1 == pytest.approx(0.025)   # 真涨留着


def test_adjusted_bars_flag_event_that_prices_do_not_support():
    # 事件说该跌 5%，裸价实际跌了 40% ⇒ 前收盘取错或另有未登记的除权，必须标 suspect
    bars = _bars([("2024-05-30", 20.0), ("2024-06-03", 12.0)])
    events = [{"code": "000001", "ex_date": "2024-06-03", "cash": 1.0, "bonus": 0.0,
               "transfer": 0.0, "allot_price": 0.0, "allot_ratio": 0.0}]
    steps = build_factors(bars, events)
    assert steps[0]["suspect"] is True


def test_unexplained_gaps_finds_exrights_missing_from_the_calendar():
    bars = _bars([("2024-05-30", 20.0), ("2024-05-31", 14.0), ("2024-06-03", 14.2)])
    gaps = unexplained_gaps(bars, [], limit_pct=0.10)
    assert [g["date"] for g in gaps] == ["2024-05-31"]
    assert gaps[0]["raw_ret"] == pytest.approx(-0.30)


def test_unexplained_gaps_ignores_days_the_calendar_already_covers():
    bars = _bars([("2024-05-30", 20.0), ("2024-06-03", 14.0)])
    events = [{"code": "000001", "ex_date": "2024-06-03", "cash": 0.0, "bonus": 0.0,
               "transfer": 0.4, "allot_price": 0.0, "allot_ratio": 0.0}]
    assert unexplained_gaps(bars, build_factors(bars, events), limit_pct=0.10) == []
