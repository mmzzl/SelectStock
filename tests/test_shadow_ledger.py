"""shadow 台账口径测试：出场重演、两种成交假设的差、熔断判定。

这个脚本是 shadow 期唯一的验收依据，所以三件事必须钉死：
1. **出场重演与引擎一致**（按收盘价判、先止损后止盈、触发行按当天收盘成交）；
2. **次开盘那一臂只换入场价**，出场判定不变——否则两臂之差就不纯粹是入场口径的差；
3. **熔断判定不能在样本不足时假通过**（放行臂 0 笔时 "d期望>0" 会因为 0−(负数) 而通过）。
"""

import importlib.util
import sys

import numpy as np
import pandas as pd
import pytest

SCRIPT = "scripts/build_shadow_ledger.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("build_shadow_ledger", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["build_shadow_ledger"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load_script()
TRIP = dict(mod.TRIP)


# ── 出场重演 ────────────────────────────────────────────────────────────────────

def test_exit_takes_stop_at_that_close():
    closes = np.array([[9.4, 9.9, 10.5]])          # 入场 10，第一天 -6% 触发止损
    assert mod._exit_from(np.array([10.0]), closes, 0.05, 0.0)[0] == pytest.approx(-0.06)


def test_exit_takes_profit_at_first_touch():
    closes = np.array([[10.0, 11.5, 12.0]])
    assert mod._exit_from(np.array([10.0]), closes, 0.05, 0.10)[0] == pytest.approx(0.15)


def test_exit_holds_to_last_close():
    closes = np.array([[10.0, 10.1, 10.2, 10.3]])
    assert mod._exit_from(np.array([10.0]), closes, 0.05, 0.0)[0] == pytest.approx(0.03)


def test_exit_skips_suspended_days():
    closes = np.array([[10.0, 10.2, np.nan, np.nan]])
    assert mod._exit_from(np.array([10.0]), closes, 0.05, 0.0)[0] == pytest.approx(0.02)


def test_exit_same_closes_different_entry_is_the_fill_difference():
    """同一串收盘，换入场价就是全部的差：这一条是 shadow 两臂可比性的基础。"""
    closes = np.array([[10.5, 10.6, 10.7]])
    from_close = mod._exit_from(np.array([10.0]), closes, 0.05, 0.0)[0]
    from_open = mod._exit_from(np.array([10.6]), closes, 0.05, 0.0)[0]
    assert from_close == pytest.approx(0.07)
    assert from_open == pytest.approx(10.7 / 10.6 - 1.0)


# ── 汇总与熔断 ─────────────────────────────────────────────────────────────────

def _detail(rows: list[tuple]) -> pd.DataFrame:
    """(gate, ret_close_pct, ret_open_pct) → 明细表；gate=1 放行、0 剔除。"""
    return pd.DataFrame([{
        "date": f"2026-09-{i % 28 + 1:02d}", "code": f"{i:06d}", "strategy": "pattern_reversal",
        "gate": g, "pct": 95.0 if g == 1 else 40.0,
        "ret_close_pct": c, "ret_open_pct": o,
    } for i, (g, c, o) in enumerate(rows)])


def test_summary_reports_both_arms_and_both_fill_assumptions():
    d = _detail([(1, 5.0, 5.5)] * 30 + [(1, -2.0, -2.2)] * 30 + [(0, 1.0, 0.9)] * 40)
    s = mod.summarize(d, TRIP)
    assert s["arms"]["pass"]["close"]["n"] == 60
    assert s["arms"]["reject"]["close"]["n"] == 40
    # 次开盘那一臂的笔数与收盘口径相同（只换入场价，不换样本）
    assert s["arms"]["pass"]["open"]["n"] == 60
    assert s["arms"]["pass"]["close"]["win_rate"] == pytest.approx(0.5)
    assert s["arms"]["pass"]["open"]["win_rate"] == pytest.approx(0.5)


def test_summary_trips_when_pass_arm_too_small():
    """放行臂 27 笔（近 40 天的真实量级）必须判样本不足，不能因为 d期望为正就放行。"""
    d = _detail([(1, -4.0, -4.0)] * 27 + [(0, -2.0, -2.0)] * 200)
    s = mod.summarize(d, TRIP)
    assert s["verdict"] == "trip" and "笔数够" in s["failed_checks"]
    assert "d期望>0" in s["failed_checks"]      # 放行臂 0 笔时这一格不能假通过


def test_summary_trips_when_expectancy_negative():
    d = _detail([(1, -3.0, -3.0)] * 100 + [(0, -1.0, -1.0)] * 100)
    s = mod.summarize(d, TRIP)
    assert s["verdict"] == "trip"
    assert {"单笔期望>0", "d期望>0"} <= set(s["failed_checks"])


def test_summary_passes_when_gate_helps_and_fill_is_not_broken():
    d = _detail([(1, 6.0, 6.2)] * 40 + [(1, -2.0, -2.1)] * 40 + [(0, 0.5, 0.4)] * 200)
    s = mod.summarize(d, TRIP)
    assert s["verdict"] == "ok", s["failed_checks"]
    # 次开盘更贵（正 drag）超过 0.5pp 就要熔断：说明回测的收盘假设已经不可信
    worse = _detail([(1, 6.0, 5.0)] * 80 + [(0, 0.5, 0.4)] * 200)
    assert "成交假设未崩" in mod.summarize(worse, TRIP)["failed_checks"]


def test_payoff_is_none_when_one_side_has_no_samples():
    """全胜或全输时赔率/利润因子**估不出来**，必须是 None（页面印「-」），不是 0。"""
    all_win = _detail([(1, 3.0, 3.0)] * 80 + [(0, 0.5, 0.5)] * 200)
    s = mod.summarize(all_win, TRIP)
    assert s["arms"]["pass"]["close"]["payoff"] is None
    assert "赔率" in s["failed_checks"]


def test_render_marks_each_check_and_never_prints_nan():
    d = _detail([(1, 3.0, 3.0)] * 80 + [(0, 0.5, 0.5)] * 200)
    text = mod.render(mod.summarize(d, TRIP))
    assert "nan" not in text and "✓ 笔数够" in text and "熔断判定" in text
