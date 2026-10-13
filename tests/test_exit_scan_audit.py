"""成交可执行性审计的口径测试：板块阈值、一字与炸板分开数、亏损只累加负 pnl。"""

import importlib.util
import sys

import pytest

SCRIPT = "scripts/backtest_exit_scan.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("backtest_exit_scan", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["backtest_exit_scan"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load_script()


class _Provider:
    """只实现 rows()/_load()：审计只用得到当天那一行的 pct/high/low，以及「下一天」。"""

    def __init__(self, rows):
        self._rows = rows

    def rows(self, date, codes=None):
        return {c: self._rows[(date, c)] for c in (codes or []) if (date, c) in self._rows}

    def _load(self, code):
        return None          # 没有次日可查：默认测试只验计数，次晨差另测


def _audit(rows, trades):
    return mod._fill_audit(_Provider(rows), trades)


def test_main_board_limit_down_sell_counted_and_loss_share():
    provider_rows = {
        ("2026-01-05", "000001"): {"pct": -10.0, "high": 9.0, "low": 9.0},
        ("2026-01-06", "000001"): {"pct": -3.0, "high": 10.0, "low": 9.5},
    }
    trades = [
        {"side": "sell", "date": "2026-01-05", "code": "000001", "pnl": -500.0},
        {"side": "sell", "date": "2026-01-06", "code": "000001", "pnl": -500.0},
    ]
    out = _audit(provider_rows, trades)
    assert out["sell_n"] == 2 and out["ld_sell"] == 1
    assert out["ld_loss"] == 500.0 and out["loss_sum"] == 1000.0
    assert out["one_sell"] == 1          # high==low：一字跌停，当天完全出不了货


def test_gem_limit_is_twenty_percent_so_minus_twelve_is_not_blocked():
    """阈值必须板块感知：创业板 −12% 离跌停还远，算成「卖不掉」会把审计变成噪声。"""
    out = _audit({("2026-01-05", "300123"): {"pct": -12.0, "high": 12.0, "low": 10.0}},
                 [{"side": "sell", "date": "2026-01-05", "code": "300123", "pnl": -300.0}])
    assert out["ld_sell"] == 0 and out["loss_sum"] == 300.0
    out2 = _audit({("2026-01-05", "300123"): {"pct": -19.6, "high": 11.0, "low": 10.5}},
                  [{"side": "sell", "date": "2026-01-05", "code": "300123", "pnl": -300.0}])
    assert out2["ld_sell"] == 1


def test_limit_up_buy_and_sealed_buy_are_separate_counts():
    """收在涨停线（可能炸板、还能买进，只是追高）与一字封死（买不到）不能混成一个数。"""
    rows = {
        ("2026-01-05", "000001"): {"pct": 10.0, "high": 11.0, "low": 10.6},   # 炸板/盘中能成交
        ("2026-01-06", "000001"): {"pct": 10.0, "high": 12.0, "low": 12.0},   # 一字
    }
    trades = [{"side": "buy", "date": d, "code": c, "pnl": 0.0} for d, c in rows]
    out = _audit(rows, trades)
    assert out["buy_n"] == 2 and out["lu_buy"] == 2 and out["one_buy"] == 1


def test_profitable_sell_adds_to_sell_count_not_to_loss_sum():
    out = _audit({("2026-01-05", "000001"): {"pct": 8.0, "high": 12.0, "low": 11.0}},
                 [{"side": "sell", "date": "2026-01-05", "code": "000001", "pnl": 400.0}])
    assert out["sell_n"] == 1 and out["loss_sum"] == 0.0 and out["ld_loss"] == 0.0


def test_missing_row_is_skipped_not_treated_as_blocked():
    """停牌/取不到日线时宁可不计，也不能把一笔成交默认判成「出不了货」。"""
    out = _audit({}, [{"side": "sell", "date": "2026-01-05", "code": "000001", "pnl": -100.0}])
    assert out["sell_n"] == 0 and out["ld_sell"] == 0 and out["loss_sum"] == 0.0
    assert out["no_row"] == 1


def test_add_side_counts_as_an_entry():
    out = _audit({("2026-01-05", "000001"): {"pct": 0.0, "high": 11.0, "low": 10.0}},
                 [{"side": "add", "date": "2026-01-05", "code": "000001", "pnl": 0.0}])
    assert out["buy_n"] == 1


def test_entry_gap_is_next_open_over_fill_price(monkeypatch):
    """次晨开盘差是「回测白拿的进场优势」，换算成元要按成交金额，不是按 1 元。"""
    rows = {("2026-01-05", "000001"): {"pct": 0.0, "high": 11.0, "low": 10.0}}
    monkeypatch.setattr(mod, "_next_row",
                        lambda provider, date, code: {"open": 10.3})
    out = _audit(rows, [{"side": "buy", "date": "2026-01-05", "code": "000001",
                         "price": 10.0, "amount": 15000.0, "pnl": 0.0}])
    assert out["gap_sum"] == pytest.approx(0.03)
    assert out["gap_cost_sum"] == pytest.approx(450.0)      # 3% × 15000 元
    assert out["gap_lu_n"] == 0                            # 没到涨停线，不进涨停单那栏


def test_entry_gap_skipped_without_next_day(monkeypatch):
    rows = {("2026-01-05", "000001"): {"pct": 10.0, "high": 11.0, "low": 10.0}}
    monkeypatch.setattr(mod, "_next_row", lambda provider, date, code: None)
    out = _audit(rows, [{"side": "buy", "date": "2026-01-05", "code": "000001",
                         "price": 10.0, "amount": 15000.0, "pnl": 0.0}])
    assert out["lu_buy"] == 1 and out["gap_n"] == 0 and out["gap_cost_sum"] == 0.0
