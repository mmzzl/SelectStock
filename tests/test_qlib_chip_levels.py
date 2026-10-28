"""qlib 侧的筹码价位：从 bin 面板取「信号日及之前」的 60 根，交给 `features/chips.py` 算三档价。

守三件事：
1. **取数窗口右端必须钉在信号日**（end_time=信号日）——面板里已经有明天的行时也不能看，
   这条一旦漂了，名单的买入价就变成用未来价算的；
2. 历史不够就是没有价位（那个键直接缺席），绝不返回一个补出来的价；
3. 价位只由窗口内的行决定：窗口右端换个日子，结果必须跟着换。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from stock_selector_qlib import serving  # noqa: E402

DAY = "2026-10-27"
DAYS = [f"2026-10-{d:02d}" for d in range(1, 28)]


def _bars(low: float, high: float, close: float, days: list[str],
          amount: float = 5e8) -> pd.DataFrame:
    return pd.DataFrame([{"date": d, "low": low, "high": high, "close": close, "amount": amount}
                         for d in days])


def _panel_frame(dates: list[str], symbols: list[str], low=13.5, high=14.5, close=14.0):
    rows = [{"datetime": pd.Timestamp(d), "instrument": s,
             "$high": high, "$low": low, "$close": close, "$amount": 5e8}
            for d in dates for s in symbols]
    return pd.DataFrame(rows).set_index(["datetime", "instrument"]).sort_index()


@pytest.fixture
def cal(monkeypatch):
    monkeypatch.setattr(serving, "calendar", lambda: DAYS)
    return DAYS


def test_panel_bars_pins_the_window_to_the_signal_day(monkeypatch, cal):
    seen = {}

    def fake_features(symbols, fields, beg, date):
        seen["fields"], seen["beg"], seen["date"] = fields, beg, date
        return _panel_frame(cal, list(symbols))

    monkeypatch.setattr(serving, "_d_features", fake_features)

    bars = serving.panel_bars(["SH600000"], DAY, window=20)
    assert seen["date"] == DAY                           # 右端钉死在信号日：绝不看明天
    assert seen["beg"] == "2026-10-07"                   # 往前 20 个交易日
    assert {"$high", "$low", "$close", "$amount"} <= set(seen["fields"])
    frame = bars["SH600000"]
    assert frame["date"].iloc[-1] == DAY                 # 最后一根就是信号日
    assert {"low", "high", "close", "amount"} <= set(frame.columns)
    assert len(frame) == 21                              # 窗口 + 一根前收（ATR 的真实波幅要用）


def test_chip_levels_returns_buy_target_and_stop(monkeypatch):
    monkeypatch.setattr(serving, "panel_bars",
                        lambda symbols, date, window: {"SH600000": _bars(13.5, 14.5, 14.0, DAYS)})
    lv = serving.chip_levels(["SH600000"], DAY, window=20)["SH600000"]
    assert lv["buy_ref_price"] == pytest.approx(13.5)    # 主峰下沿 = 买入区间
    assert lv["sell_target_price"] == pytest.approx(14.5)  # 主峰上沿 = 第一止盈
    assert lv["buy_invalid_price"] < lv["buy_ref_price"]  # 止损挂在买入价下方
    assert lv["close"] == pytest.approx(14.0)


def test_chip_levels_omits_names_without_enough_history(monkeypatch):
    monkeypatch.setattr(serving, "panel_bars", lambda symbols, date, window: {
        "SH600000": _bars(13.5, 14.5, 14.0, DAYS[:6]),    # 只有 6 根，算不了 20 日主峰
        "SZ000001": None,                                  # 面板里这只整段缺
    })
    assert serving.chip_levels(["SH600000", "SZ000001"], DAY, window=20) == {}


def test_chip_levels_moves_with_the_window_right_edge(monkeypatch):
    """后 7 天抬高到 20 元：窗口切在 10-20 时主峰必须还在 14 一带，不能被未来段抬走。"""
    up = pd.concat([_bars(13.5, 14.5, 14.0, DAYS[:20]),
                    _bars(20.0, 21.0, 20.5, DAYS[20:])], ignore_index=True)
    monkeypatch.setattr(serving, "panel_bars",
                        lambda symbols, date, window: {"SH600000": up.iloc[:20]})
    lv = serving.chip_levels(["SH600000"], "2026-10-20", window=20)["SH600000"]
    assert lv["chip_peak_high"] < 15.0
    assert lv["close"] == pytest.approx(14.0)


def test_chip_levels_never_leaks_a_price_from_after_the_signal_day(monkeypatch):
    """后 7 天不但抬价、还放大量：切在 10-20 的主峰必须仍留在 14 一带，不能被后面那段抬走。"""
    up = pd.concat([_bars(13.5, 14.5, 14.0, DAYS[:20]),
                    _bars(20.0, 21.0, 20.5, DAYS[20:], amount=3e9)], ignore_index=True)

    def fake_bars(symbols, date, window):
        cut = {"2026-10-20": 20, "2026-10-27": len(DAYS)}[date]
        return {"SH600000": up.iloc[:cut]}

    monkeypatch.setattr(serving, "panel_bars", fake_bars)
    early = serving.chip_levels(["SH600000"], "2026-10-20", window=20)["SH600000"]
    late = serving.chip_levels(["SH600000"], "2026-10-27", window=20)["SH600000"]
    assert early["buy_ref_price"] < 15.0                 # 信号日之前只有 13.5~14.5 这段成交
    assert late["buy_ref_price"] > 20.0                  # 同一只票换到 10-27，主峰已经在 20 上方
    assert early["close"] == pytest.approx(14.0) and late["close"] == pytest.approx(20.5)
