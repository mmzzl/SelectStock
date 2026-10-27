"""FF-1 预注册读数脚本的判据单测（口径见 `stock_selector/docs/资金流数据口径与防前瞻红线.md` §八）。

这些用例只喂内存里的小表，不读 qlib、不连 Mongo、不发钉钉。
防前瞻那条自求断言（删掉未来行重算必须逐位相同）是这份读数最关键的一道锁。
"""
from __future__ import annotations

import importlib.util
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def ff():
    spec = importlib.util.spec_from_file_location(
        "fund_flow_signal_read", str(ROOT / "scripts" / "fund_flow_signal_read.py"))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def rows(items: list[tuple[str, str, float, float]]) -> pd.DataFrame:
    """(code, trade_date, amount, main_net_inflow) → 分区表。"""
    df = pd.DataFrame(items, columns=["code", "trade_date", "amount", "main_net_inflow"])
    df["valid"] = True
    df["exclude_reason"] = None
    return df


# ---------------------------------------------------------------------------
# 信号：ratio_1d / ratio_3d
# ---------------------------------------------------------------------------

def test_ratio_3d_is_amount_weighted_sum_not_mean_of_ratios(ff):
    got = ff.signal_frame(rows([
        ("600001", "2026-04-14", 100.0, 10.0),
        ("600001", "2026-04-15", 300.0, -30.0),
        ("600001", "2026-04-16", 100.0, 0.0),
    ])).set_index("trade_date")
    # 加权 = (10-30+0)/(100+300+100) = -0.04；简单平均会是 (0.1-0.1+0)/3 = 0 —— 两者必须分开
    assert got.loc["2026-04-16", "ratio_3d"] == pytest.approx(-0.04)
    assert got.loc["2026-04-16", "ratio_1d"] == pytest.approx(0.0)
    assert math.isnan(got.loc["2026-04-14", "ratio_3d"])     # 窗口不满 3 天不硬凑


def test_signal_recomputes_identically_when_future_rows_are_dropped(ff):
    """防前瞻自求：只用 ≤T 的行，把 T 之后的行删掉重算，T 及以前的信号必须逐位相同。"""
    data = rows([(f"60000{i}", d, 100.0, (i - 2) * 3.0)
                 for i in range(5)
                 for d in ("2026-04-14", "2026-04-15", "2026-04-16", "2026-04-17")])
    full = ff.signal_frame(data).set_index(["code", "trade_date"]).sort_index()
    past = ff.signal_frame(data[data["trade_date"] <= "2026-04-16"]) \
        .set_index(["code", "trade_date"]).sort_index()
    joined = past.join(full, rsuffix="_full", how="left")
    assert len(joined) == len(past)
    for col in ("ratio_1d", "ratio_3d"):
        assert joined[col].isna().equals(joined[f"{col}_full"].isna()), f"{col} 的可得性变了"
        live = joined[col].notna()
        assert live.any()
        assert (joined.loc[live, col] == joined.loc[live, f"{col}_full"]).all(), f"{col} 吃到了未来行"


def test_ratio_3d_is_nan_until_three_days_and_never_filled_with_zero(ff):
    got = ff.signal_frame(rows([
        ("600002", "2026-04-14", 100.0, 5.0),
        ("600002", "2026-04-15", 100.0, 5.0),
    ])).set_index("trade_date")
    assert math.isnan(got.loc["2026-04-15", "ratio_3d"])   # 只有 2 天 → 判不了，不是 0
    assert got.loc["2026-04-15", "ratio_1d"] == pytest.approx(0.05)


def test_ratio_3d_is_nan_when_turnover_sum_is_not_positive(ff):
    got = ff.signal_frame(rows([
        ("600003", d, 0.0, 0.0)
        for d in ("2026-04-14", "2026-04-15", "2026-04-16")
    ])).set_index("trade_date")
    assert math.isnan(got.loc["2026-04-16", "ratio_3d"])
    assert math.isnan(got.loc["2026-04-16", "ratio_1d"])


def test_signals_do_not_leak_across_stocks(ff):
    got = ff.signal_frame(rows([
        ("600004", "2026-04-14", 100.0, 50.0),
        ("600005", "2026-04-14", 100.0, -50.0),
        ("600004", "2026-04-15", 100.0, 0.0),
        ("600005", "2026-04-15", 100.0, 0.0),
        ("600004", "2026-04-16", 100.0, 25.0),
        ("600005", "2026-04-16", 100.0, -25.0),
    ])).set_index(["code", "trade_date"])
    assert got.loc[("600004", "2026-04-16"), "ratio_3d"] == pytest.approx(0.25)
    assert got.loc[("600005", "2026-04-16"), "ratio_3d"] == pytest.approx(-0.25)
    assert got.loc[("600004", "2026-04-16"), "ratio_1d"] == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# §4′ 可成交性 · 买入侧
# ---------------------------------------------------------------------------

def test_entry_state_splits_open_sealed_from_later_sealed(ff):
    assert ff.entry_state(10.0, 11.0, 11.0) == "sealed_open"      # 开盘 +10% → 买不进
    assert ff.entry_state(10.0, 10.5, 11.0) == "opened_then_sealed"  # 开盘没封、盘中才封 → 能买
    assert ff.entry_state(10.0, 10.2, 10.4) == "tradable"
    assert ff.entry_state(10.0, float("nan"), float("nan")) == "no_bar"   # T+1 停牌


def test_entry_state_uses_the_frozen_98_tolerance(ff):
    assert ff.entry_state(10.0, 10.98, 10.98) == "sealed_open"    # 恰好 9.8% 算封住
    assert ff.entry_state(10.0, 10.979, 10.979) == "tradable"     # 差 0.1 分钱不算


# ---------------------------------------------------------------------------
# §4′ 可成交性 · 卖出侧（跌停顺延是真代价，不是换个价假装成交）
# ---------------------------------------------------------------------------

def test_exit_at_close_when_limit_down_opened_intraday(ff):
    bars = [{"open": 9.3, "high": 9.6, "close": 9.0}]            # 收盘跌停，但盘中打开过
    plan = ff.exit_plan(10.0, bars)
    assert plan["reason"] == "closed_limit"
    assert plan["delay_days"] == 0
    assert plan["price"] == pytest.approx(9.0)


def test_exit_defers_when_the_whole_session_is_sealed_limit_down(ff):
    bars = [{"open": 9.0, "high": 9.0, "close": 9.0},            # 全天封死：卖不出
            {"open": 8.6, "high": 8.9, "close": 8.8}]            # 第二天开盘卖
    plan = ff.exit_plan(10.0, bars)
    assert plan["reason"] == "deferred"
    assert plan["delay_days"] == 1
    assert plan["price"] == pytest.approx(8.6)                   # 顺延后用下一个可成交日的开盘价
    assert plan["extra_drop"] == pytest.approx(8.6 / 9.0 - 1)    # 比原定出场多吃的跌幅


def test_exit_defers_over_a_suspended_session(ff):
    bars = [{"open": np.nan, "high": np.nan, "close": np.nan},   # 停牌：没有 bar
            {"open": 9.1, "high": 9.4, "close": 9.3}]
    plan = ff.exit_plan(10.0, bars)
    assert plan["reason"] == "deferred"
    assert plan["delay_days"] == 1
    assert plan["price"] == pytest.approx(9.1)


def test_exit_truncates_after_ten_deferred_sessions_and_says_so(ff):
    sealed = [{"open": 10.0 * 0.9 ** (i + 1), "high": 10.0 * 0.9 ** (i + 1),
               "close": 10.0 * 0.9 ** (i + 1)} for i in range(14)]   # 连续一字跌停，天天卖不出
    plan = ff.exit_plan(10.0, sealed, max_delay=10)
    assert plan["reason"] == "truncated"
    assert plan["delay_days"] == 10
    assert math.isnan(plan["price"])                                 # 前 10 天内没有任何可成交价


# ---------------------------------------------------------------------------
# 统计口径
# ---------------------------------------------------------------------------

def test_deciles_are_ranked_within_the_same_day_only(ff):
    day = pd.Series([1.0, 2.0, 3.0], index=["60000" + str(i) for i in (1, 2, 3)])
    got = ff.assign_deciles(day)
    assert got.isin(range(1, 11)).all()
    assert got["600003"] == 10                                    # 当日最高的那只落进 D10
    assert got["600001"] < got["600002"] < got["600003"]           # 名次只跟当天比
    assert ff.assign_deciles(pd.Series([float("nan")], index=["600001"])).isna().all()


def test_deciles_put_the_largest_value_in_the_top_bucket(ff):
    values = pd.Series(np.arange(100.0))
    got = ff.assign_deciles(values)
    assert (got == 10).sum() == 10
    assert got[values.idxmax()] == 10
    assert got[values.idxmin()] == 1


def test_adjusted_t_divides_the_daily_t_by_sqrt_10(ff):
    diffs = pd.Series([float(i) for i in range(1, 12)])          # mean 6、sd(ddof=1) √11 → t=6
    assert ff.adjusted_t(diffs) == pytest.approx(6.0 / math.sqrt(10))
    assert ff.adjusted_t(pd.Series([1.0, -1.0, 1.0, -1.0])) < 1.0


def test_adjusted_t_needs_at_least_two_daily_points(ff):
    assert math.isnan(ff.adjusted_t(pd.Series([1.0])))


# ---------------------------------------------------------------------------
# 门槛裁决（§八.5 写死，不许读数后回头改）
# ---------------------------------------------------------------------------

def mk(t=3.0, days=99_000, rho=0.9, sign=1.0, holdout_sign=1.0) -> dict:
    return {"t_adj": t, "min_bucket_days": days, "rho": rho,
            "sign_main": sign, "sign_holdout": holdout_sign}


def test_signal_pass_requires_all_three_gates(ff):
    assert ff.signal_pass(mk()) == "过"
    assert ff.signal_pass(mk(t=1.9)) == "不过"
    assert ff.signal_pass(mk(days=2_999)) == "不过"
    assert ff.signal_pass(mk(rho=0.59)) == "不过"


def test_signal_pass_needs_monotony_to_agree_with_the_spread(ff):
    assert ff.signal_pass(mk(t=3.0, rho=-0.9)) == "不过"          # 方向打架
    assert ff.signal_pass(mk(t=-3.0, rho=-0.9)) == "过"           # 反向单调也算数


def test_signal_flipping_in_the_holdout_is_unstable_not_passed(ff):
    assert ff.signal_pass(mk(sign=1.0, holdout_sign=-1.0)) == "不稳"


def test_incremental_gate_needs_full_sample_and_out_of_sample_window(ff):
    assert ff.incremental_gate(3.0, 2.5) is True
    assert ff.incremental_gate(3.0, 1.5) is False                 # 样本外段没站住
    assert ff.incremental_gate(-2.2, -3.1) is True                # 反向但两段都过线


def test_overall_verdict_wording(ff):
    both = {"ratio_1d": "过", "ratio_3d": "过"}
    assert ff.overall(both, True) == "够格接"
    assert ff.overall(both, False) == "只当展示与否决提示"
    assert ff.overall({"ratio_1d": "过", "ratio_3d": "不过"}, True) == "弱证据"
    assert ff.overall({"ratio_1d": "不稳", "ratio_3d": "过"}, True) == "弱证据"
    assert ff.overall({"ratio_1d": "不过", "ratio_3d": "不过"}, False) == "没有前向信号"


# ---------------------------------------------------------------------------
# adjusted_frames：除权日必须折成东财口径；停牌冻结行（volume=0、价抄前一天）必须作废
# ---------------------------------------------------------------------------

def _wide(days, series):
    return pd.DataFrame(series, index=days)


def test_adjusted_frames_reproduces_em_returns_across_ex_rights(ff):
    days = ["d1", "d2", "d3"]
    insts = ["SH600001", "SH600002"]
    raw = {
        "$open": _wide(days, {"SH600001": [9.95, 9.10, 10.10], "SH600002": [8.00, 8.00, 7.70]}),
        "$high": _wide(days, {"SH600001": [10.05, 9.30, 10.40], "SH600002": [8.10, 8.00, 7.80]}),
        "$close": _wide(days, {"SH600001": [10.00, 9.00, 10.30], "SH600002": [8.00, 8.00, 7.60]}),
        "$last_close": _wide(days, {"SH600001": [9.90, 10.00, 9.00], "SH600002": [8.00, 8.00, 8.00]}),
        "$volume": _wide(days, {"SH600001": [100.0, 120.0, 130.0], "SH600002": [90.0, 0.0, 70.0]}),
    }
    pct = _wide(days, {"SH600001": [1.0, 2.0, 1.0],      # d2/d3 是除权日：裸价说 −10%/+14.4%
                       "SH600002": [0.0, np.nan, -5.0]})
    adj, stats = ff.adjusted_frames(raw, pct)
    got = adj["$close"] / adj["$close"].shift(1) - 1.0
    assert abs(got.loc["d2", "SH600001"] - 0.02) < 1e-9
    assert abs(got.loc["d3", "SH600001"] - 0.01) < 1e-9
    assert stats["除权修正格数"] == 2


def test_adjusted_frames_voids_frozen_suspended_rows(ff):
    days = ["d1", "d2", "d3"]
    insts = ["SH600001", "SH600002"]
    raw = {
        "$open": _wide(days, {"SH600001": [9.95, 9.10, 10.10], "SH600002": [8.00, 8.00, 7.70]}),
        "$high": _wide(days, {"SH600001": [10.05, 9.30, 10.40], "SH600002": [8.10, 8.00, 7.80]}),
        "$close": _wide(days, {"SH600001": [10.00, 9.00, 10.30], "SH600002": [8.00, 8.00, 7.60]}),
        "$last_close": _wide(days, {"SH600001": [9.90, 10.00, 9.00], "SH600002": [8.00, 8.00, 8.00]}),
        "$volume": _wide(days, {"SH600001": [100.0, 120.0, 130.0], "SH600002": [90.0, 0.0, 70.0]}),
    }
    pct = _wide(days, {"SH600001": [1.0, 2.0, 1.0], "SH600002": [0.0, np.nan, -5.0]})
    adj, stats = ff.adjusted_frames(raw, pct)
    # 冻结行不是行情：不能当成「平盘且可买卖」的那天
    for field in ("$open", "$high", "$close"):
        assert pd.isna(adj[field].loc["d2", "SH600002"]), field
    assert stats["停牌冻结格数"] == 1
    # 复牌那天照旧可用
    assert abs(adj["$close"].loc["d3", "SH600002"] / 8.0 - 1.0 - (-0.05)) < 1e-9


def test_incremental_readings_returns_nan_not_crash_when_bands_too_small(ff):
    """档内不足 40 只（小样本冒烟）时：给 NaN 读数，不许在空序列上做日期比较。"""
    recs = pd.DataFrame({
        "trade_date": ["2026-04-14"] * 6, "instrument": [f"SH60000{i}" for i in range(6)],
        "hold": 5, "ok": True, "ratio_1d": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6],
        "net": [0.01, -0.01, 0.02, -0.02, 0.03, 0.0],
    })
    score = pd.Series([5, 4, 3, 2, 1, 0], dtype=float,
                      index=pd.MultiIndex.from_arrays(
                          [recs["trade_date"], recs["instrument"]]))
    out = ff.incremental_readings(recs, "ratio_1d", score)
    assert out["days"] == 0
    assert math.isnan(out["t_adj"]) and math.isnan(out["t_out_of_sample"])


# ---------------------------------------------------------------------------
# 板块口径与「整段塌了」的采集洞（都是读数前就能判的数据侧规则，不看任何收益）
# ---------------------------------------------------------------------------

def test_board_ok_keeps_only_tradable_main_board(ff):
    for code in ("600000", "601398", "603986", "605296", "000001", "001979", "002813", "003816"):
        assert ff.board_ok(code), code
    for code in ("300750", "301158", "302132", "688981", "689009",      # 无权限段
                 "920000", "430047", "832000", "873169", "810000"):     # 北交所/新三板：±30% 不是这条 9.8% 线
        assert not ff.board_ok(code), code


def test_hole_days_flags_a_one_day_segment_collapse_not_a_regime_change(ff):
    idx = [f"d{i:02d}" for i in range(24)]
    plateau = pd.Series([3000] * 24, index=idx)
    plateau.iloc[10] = 1406                      # 2026-05-21：沪市整段没采到
    assert ff.hole_days(plateau) == ["d10"]
    step = pd.Series([2173] * 12 + [3021] * 12, index=idx)   # 4 月到 5 月的台阶：本地一致，不算洞
    assert ff.hole_days(step) == []


def test_hole_days_ignores_the_two_ends_where_there_is_no_local_median(ff):
    idx = [f"d{i:02d}" for i in range(24)]
    s = pd.Series([3000] * 24, index=idx)
    s.iloc[0] = 900                              # 窗口第一天前向没有参照：只报不判
    s.iloc[-1] = 900
    assert ff.hole_days(s) == []
