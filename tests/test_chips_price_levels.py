"""筹码主峰价位块（买入区间 / 第一止盈 / 波动率止损）——用户点名的口径：

    支撑价 = 近 60 日筹码主峰下沿 → 目标买入区间
    压力价 = 近 60 日筹码主峰上沿 → 第一止盈卖出价
    止损价 = 买入价 − 2×ATR

本机原本没有独立的筹码分布实现，所以成本分布由**已有的日线**算：每日成交额均匀摊到当天
[low, high] 覆盖的价位桶上，近 60 日累加即成本分布，最密的连续段就是主峰。

防前瞻：函数只吃调用方切好的 bars（信号日及之前），内部绝不看 bars 之外的任何行。
"""
import numpy as np
import pandas as pd
import pytest

from stock_selector.features.chips import (atr14, chip_peak_band, cost_profile,
                                           levels_note, price_levels)


def bars(rows: list[tuple]) -> pd.DataFrame:
    """rows = [(date, low, high, close, amount)]，按时间升序。"""
    return pd.DataFrame([{"date": d, "low": lo, "high": hi, "close": c, "amount": a}
                         for d, lo, hi, c, a in rows])


def flat(days: int, price: float, amount: float = 1e8) -> pd.DataFrame:
    """days 根一模一样的十字星（low=high=close=price）：成本全堆在一个价位。"""
    return bars([(f"2026-01-{i:02d}", price, price, price, amount) for i in range(1, days + 1)])


def test_cost_profile_spreads_amount_over_the_day_range():
    # 两天各 1 亿，各摊自己那天的 [low, high]：14~15 只有第一天给
    df = bars([("2026-01-01", 12.0, 15.0, 13.0, 1e8), ("2026-01-02", 10.0, 11.0, 10.5, 1e8)])
    prof = cost_profile(df, bins=10)
    assert prof.index.min() >= 10.0 and prof.index.max() <= 15.0
    assert pytest.approx(prof.sum(), rel=1e-6) == 2e8          # 成交额一分不丢
    # 12 元以下的成本只能来自第二天（第一天最低 12 元），所以那一段必须正好是它的 1 亿
    assert pytest.approx(prof[prof.index < 12.0].sum(), rel=1e-6) == 1e8
    assert pytest.approx(prof[(prof.index >= 11.0) & (prof.index < 12.0)].sum(), rel=1e-9) == 0.0


def test_cost_profile_ignores_suspended_and_missing_rows():
    df = bars([("2026-01-01", 12.0, 15.0, 13.0, 1e8),
               ("2026-01-02", 13.0, 13.0, 13.0, 0.0),          # 停牌：成交额 0，不该造峰
               ("2026-01-03", 13.0, 13.0, 13.0, np.nan)])
    assert pytest.approx(cost_profile(df, bins=10).sum(), rel=1e-6) == 1e8


def test_chip_peak_band_returns_the_dense_contiguous_zone():
    df = bars([("2026-01-01", 10.0, 20.0, 15.0, 1e8),
               ("2026-01-02", 13.5, 14.5, 14.0, 5e8),          # 主峰：钱都堆在 13.5~14.5
               ("2026-01-03", 13.5, 14.5, 14.0, 5e8)])
    low, high = chip_peak_band(cost_profile(df, bins=20))
    assert 13.0 <= low <= 14.0 and 14.0 <= high <= 15.0        # 落在密集区内，不吃两端稀疏区


def test_chip_peak_band_is_a_single_bucket_on_a_spike():
    low, high = chip_peak_band(cost_profile(flat(60, 12.0), bins=40))
    assert low == pytest.approx(12.0, abs=0.01)
    assert high == pytest.approx(12.0, abs=0.01)


def test_atr14_is_the_mean_true_range_of_the_last_window_bars():
    df = bars([(f"2026-01-{i:02d}", 10.0, 12.0, 11.0, 1e8) for i in range(1, 21)])
    assert atr14(df) == pytest.approx(2.0)                     # 每天 high−low=2，且无跳空
    jump = bars([("2026-01-01", 10.0, 10.5, 10.0, 1e8), ("2026-01-02", 12.0, 13.0, 12.5, 1e8)])
    assert atr14(jump, window=2) == pytest.approx((0.5 + 3.0) / 2)   # 第二天 TR = 13 − 10 = 3


def test_price_levels_maps_peak_lower_to_buy_and_upper_to_first_target():
    df = bars([(f"d{i}", 13.5, 14.5, 14.0, 5e8) for i in range(58)]
              + [("d58", 10.0, 20.0, 15.0, 1e8), ("d59", 14.4, 14.6, 14.5, 1e8)])
    lv = price_levels(df, window=60, bins=20)
    assert lv is not None
    assert lv["buy_ref_price"] == lv["chip_peak_low"]          # 主峰下沿 = 买入区间
    assert lv["sell_target_price"] == lv["chip_peak_high"]     # 主峰上沿 = 第一止盈
    assert lv["buy_invalid_price"] == pytest.approx(lv["buy_ref_price"] - 2 * lv["atr14"],
                                                   abs=0.01)   # 两档各自四舍五入到分
    assert lv["buy_ref_price"] < lv["sell_target_price"]


def test_price_levels_reports_position_relative_to_the_peak():
    lv = price_levels(bars([(f"d{i}", 13.5, 14.5, 14.0, 5e8) for i in range(59)]
                           + [("sig", 10.0, 10.2, 10.1, 1e8)]), window=60, bins=20)
    assert lv["close_below_peak"] is True                      # 主峰在现价上方（上方套牢）
    assert lv["peak_gap_pct"] < 0.0


def test_price_levels_returns_none_when_history_is_too_thin():
    assert price_levels(flat(30, 12.0), window=60) is None     # 新票不编价位
    assert price_levels(flat(60, np.nan), window=60) is None


def test_price_levels_rounds_to_the_minimum_tick():
    lv = price_levels(bars([(f"d{i}", 13.47, 14.53, 14.0, 5e8) for i in range(60)]), bins=7)
    for key in ("buy_ref_price", "sell_target_price", "buy_invalid_price"):
        assert lv[key] > 0.0 and round(lv[key], 2) == lv[key]


def test_levels_note_names_the_rule_and_the_position():
    """口径句由价位块自己出：两个消费方（qlib 名单、持仓诊断）共用同一句，措辞不许漂移。

    必须包含：窗口、主峰上下沿、这段占的成交额比例、止损怎么算的、以及「这是规则价不是预测」。
    """
    lv = price_levels(bars([(f"2026-01-{i:02d}", 13.5, 14.5, 14.0, 5e8) for i in range(1, 60)]
                           + [("2026-01-60", 14.4, 14.6, 14.5, 1e8)]), window=60, bins=20)
    note = levels_note(lv)
    assert f"{lv['buy_ref_price']:.2f}" in note and f"{lv['sell_target_price']:.2f}" in note
    assert "60 日" in note and "ATR" in note
    assert "规则价" in note
    # 现价在主峰上方时不说「下方」，也不能凭空造一句
    assert "现价在主峰下方" not in note
    assert len(note) <= 160                        # 库里 level_note 是 VARCHAR(160)


def test_levels_note_says_when_the_peak_is_still_above_today():
    """2026-10-28 实测：当日 qlib 榜 30 只**全部**现价在主峰下方（中位差 31%）。

    只写「买入参考 58.12」会被读成今天挂得进去，而实际是等反弹进主峰才有成交——
    这句必须自带，缺数时也不能靠页面自己猜。
    """
    lv = price_levels(bars([(f"2026-01-{i:02d}", 13.5, 14.5, 14.0, 5e8) for i in range(1, 60)]
                           + [("2026-01-60", 10.0, 10.2, 10.1, 1e8)]), window=60, bins=20)
    assert lv["close_below_peak"] is True
    note = levels_note(lv)
    assert "现价在主峰下方" in note and f"{abs(lv['peak_gap_pct']):.1f}%" in note
    assert len(note) <= 160
