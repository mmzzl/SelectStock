"""批次 G2：只学「板后低开买这一笔赚不赚钱」的事件臂——事件行筛选纯函数的 RED 测试。

用户口径（2026-10-29 原话）：「模型应该学，今天涨停，或者上涨，明天低开买入后，是否赚钱」。
所以训练行**只有**满足两个条件的事件：
  ① 信号日 F 收盘涨停（$close/昨收 − 1 ≥ 0.0975）
  ② F+1 开盘相对 F 收盘低开 5% 以上（明开/今收 − 1 < −0.05）
标签 = 这笔交易的真实收益 E1 = F+1 收盘 / F+1 开盘 − 1（低开买、当天收盘卖）。

和 batchG_d1/w900 的区别：那一版把非事件也留在训练行里、标签填 0，模型学成「排掉板票」，
每天 900 只只剩 ~3 个不同分值（docs §44.2）。事件臂只在事件上训练，输出的是事件内部排序。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stock_selector_qlib.dip_event import BOARD_PCT, GAP_PCT, event_rows

DAYS = pd.date_range("2024-01-01", periods=5)


def _mk(code: str, close: list[float], open_: list[float]) -> pd.DataFrame:
    idx = pd.MultiIndex.from_product([DAYS, [code]], names=["datetime", "instrument"])
    return pd.DataFrame({"close": close, "open": open_}, index=idx)


def _panel() -> pd.DataFrame:
    """A：第 3 天板（10→11，+10%）、第 4 天低开买（开盘 10.192 = 11×0.926）当天收到 10.4 → 赚。
    B：第 3 天只涨 3%（不到板），第 4 天同样低开 → 不该进事件。
    """
    a = _mk("SH600001", [10.0, 10.0, 11.0, 10.4, 10.6], [10.0, 10.0, 10.5, 10.192, 10.6])
    b = _mk("SH600002", [10.0, 10.0, 10.3, 9.9, 10.0], [10.0, 10.0, 10.1, 9.7, 10.0])
    return pd.concat([a, b]).sort_index()


def test_only_board_and_gapdown_rows_survive():
    out = event_rows(_panel())
    assert list(out.index.get_level_values("instrument").unique()) == ["SH600001"]
    assert len(out) == 1, out.to_string()


def test_row_is_keyed_on_signal_day_and_label_is_open_to_close():
    out = event_rows(_panel())
    assert out.index.get_level_values("datetime")[0] == pd.Timestamp("2024-01-03")
    assert pytest.approx(out.iloc[0]["label"], rel=1e-9) == 10.4 / 10.192 - 1


def test_components_are_carried_for_audit():
    out = event_rows(_panel())
    row = out.iloc[0]
    assert pytest.approx(row["board_chg"], rel=1e-6) == 11.0 / 10.0 - 1
    assert pytest.approx(row["gap"], rel=1e-6) == 10.192 / 11.0 - 1


def test_signal_day_without_next_bar_is_dropped():
    """事件要 F+1 的开盘和收盘都齐才算——最后一天板、次日无行情不能编造标签。"""
    p = _panel().copy()
    p.loc[pd.Timestamp("2024-01-05"), ("SH600001", "close")] = 10.4 * 1.10   # 末日再板一次
    out = event_rows(p)
    assert (out.index.get_level_values("datetime") == pd.Timestamp("2024-01-05")).sum() == 0
    assert len(out) == 1, out.to_string()


def test_gap_exactly_at_line_is_not_an_event():
    """低开正好 −5%（=×0.95）不进事件：与 api._dip_levels 的买线 round(price*0.95) 同边界。"""
    p = _panel()
    p.loc[(pd.Timestamp("2024-01-04"), "SH600001"), "open"] = 11.0 * 0.95
    assert len(event_rows(p)) == 0


def test_thresholds_are_documented_constants():
    assert pytest.approx(BOARD_PCT) == 0.0975     # §41/F2 触发带下界
    assert pytest.approx(GAP_PCT) == -0.05        # ×0.95 低吸线，与 api._dip_levels 同源


# --- 放宽口径（2026-10-29 用户原话的字面版：「今天涨停**或者上涨**，明天**低开**买入」）-----
# 严格版（板 + 低开 5%）在 liq900 池 2016→2026 只有 553 笔（docs §44.6），训练撑不住；
# 字面版把两道阈值变成参数，同一套行本体、同一个标签。


def test_min_chg_widens_the_signal_day_gate():
    """min_chg=0.001：B 那天涨 3%（不到板）也算「上涨」，进事件。"""
    out = event_rows(_panel(), min_chg=0.001, max_gap=0.0)
    assert set(out.index.get_level_values("instrument")) == {"SH600001", "SH600002"}


def test_max_gap_widens_the_entry_gate():
    """max_gap=0.0：低开就算（不必低到 −5%）。A 次日开盘 −7.3%、B 次日 −3.9%，两笔都在。"""
    out = event_rows(_panel(), min_chg=0.0975, max_gap=0.0)
    assert list(out.index.get_level_values("instrument")) == ["SH600001"]


def test_flat_signal_day_is_not_an_uptick():
    """今收平盘（0.0%）不算「上涨」：min_chg 用 +0.1% 下界挡掉。"""
    p = _panel().copy()
    p.loc[(pd.Timestamp("2024-01-04"), "SH600002"), "close"] = 10.3   # 10.3→10.3 平盘
    p.loc[(pd.Timestamp("2024-01-05"), "SH600002"), "close"] = 10.0
    out = event_rows(p, min_chg=0.001, max_gap=0.0)
    keyed = set(zip(out.index.get_level_values("datetime"),
                    out.index.get_level_values("instrument")))
    assert (pd.Timestamp("2024-01-04"), "SH600002") not in keyed
    assert (pd.Timestamp("2024-01-03"), "SH600002") in keyed


def test_defaults_reproduce_the_strict_spec():
    """不传参数就是严格版（板 + 低开 5%），与 §44.3 那把尺子逐位一致。"""
    strict = event_rows(_panel())
    widened = event_rows(_panel(), min_chg=BOARD_PCT, max_gap=GAP_PCT)
    pd.testing.assert_frame_equal(strict, widened)


def test_panel_with_instrument_first_index_is_reordered():
    """qlib 的两个入口层级顺序不一样：`D.features` 是 (instrument, datetime)，
    handler 是 (datetime, instrument)。事件行的输出必须是后者，否则与特征表交集为空
    （batchG_evt 第一次起跑就是这样 0 笔事件、静默退化成「样本太薄」）。"""
    p = _panel().swaplevel().sort_index()
    assert p.index.names == ["instrument", "datetime"]
    out = event_rows(p)
    assert out.index.names == ["datetime", "instrument"]
    assert len(out) == 1, out.to_string()
