"""止盈止损的纯判断测试：不依赖 qlib 撮合，只守边界和缺价不造假。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from stock_selector_qlib.backtest import _make_strategy, stop_take_exits  # noqa: E402


def test_stop_take_exits_uses_entry_and_skips_missing_prices():
    held = ["A", "B", "C", "D", "E", "F"]
    entry = {"A": 100.0, "B": 100.0, "C": 100.0, "D": 100.0, "E": 100.0, "F": 0.0}
    prices = {"A": 91.99, "B": 92.0, "C": 110.0, "D": 109.99,
              "E": np.nan, "F": 80.0}
    # A -8.01%，B 正好 -8%，C 正好 +10%；D 只到 +9.99%，E 无价，F 成本为 0
    assert stop_take_exits(held, entry, prices, 0.08, 0.10) == ["A", "B", "C"]
    assert stop_take_exits(held, entry, prices, 0.08, 0.0) == ["A", "B"]
    assert stop_take_exits(held, entry, prices, 0.0, 0.10) == ["C"]
    assert stop_take_exits(held, entry, prices, 0.0, 0.0) == []


def test_make_strategy_rejects_stops_for_dropout():
    with pytest.raises(RuntimeError, match="band"):
        _make_strategy("dropout", {"topk": 1, "n_drop": 1, "stop_loss_pct": 0.08}, None)
