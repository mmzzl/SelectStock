"""信号矩阵的日历边界映射（批次 E3 前置 #169 抓到的那个缺陷，永久锁在这里）。

缺陷长这样：`np.searchsorted(calendar, dates)` 对**早于日历首日**的行也返回下标 0，
当时的过滤只看了 `pos < len(calendar)`，没核对日期等值 ⇒ 整段窗前的历史行全被钉在日历第一天，
命中表凭空多出 8.4 万条（历史窗）/ 60 万条（留出窗），那天的价格还被填成窗前最后一天的价。
它不报错、不崩，只是悄悄污染样本，所以必须有一条定点用例盯着。
"""

import importlib.util
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd

SCRIPT = "scripts/portfolio_signal_matrix.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("portfolio_signal_matrix", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["portfolio_signal_matrix"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load_script()

CALENDAR = np.array(["2022-01-04", "2022-01-05", "2022-01-06"], dtype="<U10")


# ---- calendar_positions：只有日期正好落在日历上的行才留下 ----
def test_rows_before_the_calendar_start_are_dropped_not_pinned_to_day_zero():
    dates = np.array(["2015-06-01", "2015-06-02"], dtype="<U10")
    idx, pos = mod.calendar_positions(CALENDAR, dates)
    assert idx.size == 0 and pos.size == 0


def test_rows_after_the_calendar_end_are_dropped():
    dates = np.array(["2022-01-04", "2022-01-07"], dtype="<U10")
    idx, pos = mod.calendar_positions(CALENDAR, dates)
    assert idx.tolist() == [0] and pos.tolist() == [0]


def test_gaps_inside_the_window_map_to_their_own_day():
    """票自己停牌的日子不在日历上？日历是并集，所以停牌行只是没有——这里给的是正常对齐。"""
    dates = np.array(["2022-01-03", "2022-01-05", "2022-01-06"], dtype="<U10")
    idx, pos = mod.calendar_positions(CALENDAR, dates)
    assert idx.tolist() == [1, 2] and pos.tolist() == [1, 2]


def test_searchsorted_alone_would_have_pinned_them_to_zero():
    """把缺陷本身钉在测试里：不核对等值，pos 就是 [0, 1, 2]。"""
    dates = np.array(["2015-06-01", "2022-01-04", "2022-01-05"], dtype="<U10")
    assert np.searchsorted(CALENDAR, dates).tolist() == [0, 0, 1]
    idx, pos = mod.calendar_positions(CALENDAR, dates)
    assert idx.tolist() == [1, 2] and pos.tolist() == [0, 1]


# ---- _scan_one：窗前历史行留着做回看，但不写命中与价格 ----
class FakeStrategy:
    """只要切片够长就算命中，并把每次看到的长度记下来——用来验回看窗口。"""

    def __init__(self):
        self.slices: list[int] = []

    def signal_at(self, window):
        self.slices.append(len(window))
        return SimpleNamespace(score=1.0, name=None) if len(window) >= 2 else None


def scan_one(tmp_path, rows, calendar, required_rows):
    pd.DataFrame(rows).to_parquet(tmp_path / f"{rows[0]['code']}.parquet", index=False)
    strategy = FakeStrategy()
    mod._CTX.clear()
    mod._CTX.update({"kline_dir": tmp_path, "calendar": calendar,
                     "required_rows": required_rows, "strategy": strategy})
    out = mod._scan_one(rows[0]["code"])
    return out, strategy


def test_prewindow_row_still_feeds_the_lookback_but_does_not_pollute_day_zero(tmp_path):
    rows = [{"code": "600001", "date": "2021-12-31", "high": 9.0, "low": 8.0, "close": 8.5,
             "pct": -1.0, "name": "窗前"},
            {"code": "600001", "date": "2022-01-04", "high": 10.5, "low": 9.8, "close": 10.0,
             "pct": 2.0, "name": "甲"},
            {"code": "600001", "date": "2022-01-05", "high": 11.5, "low": 10.2, "close": 11.0,
             "pct": 3.0, "name": "甲"}]
    out, strategy = scan_one(tmp_path, rows, CALENDAR, required_rows=2)
    assert out["pos"].tolist() == [0, 1]                    # 日历第一天、第二天
    assert out["close"].tolist() == [10.0, 11.0]            # 不是窗前的 8.5
    assert out["hit_pos"].tolist() == [0, 1]                # 两个命中，只落在这两天
    assert strategy.slices == [2, 2]                        # 第一天的回看窗含窗前那一行
    # 日历第三天这只票没行情 ⇒ 表里根本没有那一行（停牌＝无信号）
    assert 2 not in out["pos"].tolist()
