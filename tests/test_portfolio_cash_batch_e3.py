"""Δ 配对、档位单调性与台账计数的口径测试（批次 E3 预注册，README §6）。

这些用例钉的是**裁决尺子本身**：谁跟谁配对、什么算过线、台账记几格。
读数出来之后再改这些断言就是事后调门槛，所以必须在跑数之前锁死。
"""

import importlib.util
import sys

import pytest

SCRIPT = "scripts/portfolio_cash_batch.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("portfolio_cash_batch", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["portfolio_cash_batch"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load_script()


def row(cell, kind, reserve_pct, tranches, annual, dd, **extra):
    return {"cell": cell, "kind": kind, "reserve_pct": reserve_pct, "tranches": tranches,
            "annual_return_pp": annual, "max_drawdown_pp": dd, **extra}


# 预注册的 10 趟：基准 + 6 实验格 + 3 同档位躺平对照格（C10/C20/C30）
ALL_ROWS = [
    row("基准", "baseline", 0.0, 0, -10.0, -70.0),
    row("10%x2", "experimental", 0.10, 2, -8.0, -66.0),
    row("20%x2", "experimental", 0.20, 2, -5.0, -60.0),
    row("30%x2", "experimental", 0.30, 2, -9.0, -65.0),
    row("10%x3", "experimental", 0.10, 3, -7.5, -65.0),
    row("20%x3", "experimental", 0.20, 3, -6.0, -62.0),
    row("30%x3", "experimental", 0.30, 3, -10.0, -68.0),
    row("C10", "control", 0.10, 0, -9.0, -67.0),
    row("C20", "control", 0.20, 0, -8.0, -64.0),
    row("C30", "control", 0.30, 0, -8.0, -66.0),
]
BY_NAME = {r["cell"]: r for r in ALL_ROWS}
EXP = [r for r in ALL_ROWS if r["kind"] == "experimental"]


def rows_without(*names):
    return [r for r in ALL_ROWS if r["cell"] not in names]


# ---- 格子命名与预注册格子表 ----
def test_cell_names_separate_experimental_from_control():
    assert mod.cell_name(0.0, 0) == "基准"
    assert mod.cell_name(0.20, 2) == "20%x2"
    assert mod.cell_name(0.20, 0) == "C20"
    assert mod.control_name(0.10) == "C10"


def test_preregistered_grid_is_six_experimental_plus_three_controls():
    assert [mod.cell_name(r, t) for r, t in mod.EXPERIMENTAL] == \
        ["10%x2", "10%x3", "20%x2", "20%x3", "30%x2", "30%x3"]
    # 2026-10-17 的预注册补正：三个档位都要有同档位躺平对照，否则 Δ 单调性判不了
    assert [mod.cell_name(r, 0) for r in mod.CONTROL_RESERVES] == ["C10", "C20", "C30"]
    assert mod.N_CELLS == 9


# ---- Δ 配对：同档位是唯一许可的配对 ----
def test_delta_pairs_only_the_same_reserve_level():
    d = mod.delta(BY_NAME["20%x2"], BY_NAME["C20"])
    assert d["control"] == "C20"
    assert d["delta_annual_pp"] == pytest.approx(3.0)               # −5 − (−8)
    assert d["delta_dd_improve_pp"] == pytest.approx(4.0)           # |−64| − |−60|


def test_cross_level_pairing_is_a_hard_error():
    with pytest.raises(ValueError, match="跨档位"):
        mod.delta(BY_NAME["20%x2"], BY_NAME["C10"])                 # 20%x2 vs C10
    with pytest.raises(ValueError, match="跨档位"):
        mod.delta(BY_NAME["30%x3"], BY_NAME["C20"])                  # 30%x3 vs C20


def test_delta_requires_the_right_cell_kinds():
    with pytest.raises(ValueError, match="不是实验格"):
        mod.delta(BY_NAME["基准"], BY_NAME["C20"])                   # 基准不能当分子
    with pytest.raises(ValueError, match="不是纯躺平对照格"):
        mod.delta(BY_NAME["20%x2"], BY_NAME["10%x2"])                # 实验格不能当分母


def test_every_level_pairs_with_its_own_control_and_never_borrows():
    deltas, unpaired = mod.pair_deltas(ALL_ROWS)
    assert unpaired == []
    assert [(d["cell"], d["control"]) for d in deltas] == \
        [("10%x2", "C10"), ("20%x2", "C20"), ("30%x2", "C30"),
         ("10%x3", "C10"), ("20%x3", "C20"), ("30%x3", "C30")]


def test_missing_control_is_reported_not_borrowed_from_another_level():
    deltas, unpaired = mod.pair_deltas(rows_without("C10"))
    assert {d["cell"] for d in deltas} == {"20%x2", "20%x3", "30%x2", "30%x3"}
    assert sorted(unpaired) == ["10%x2", "10%x3"]
    assert all(d["control"] != "C10" for d in deltas)


# ---- E3-3：唯一硬指标 ----
def test_e33_needs_both_annual_and_drawdown_on_the_20_pair():
    deltas, _ = mod.pair_deltas(ALL_ROWS)
    assert mod.e33_decision(deltas)["verdict"] == "过"       # 20%x2：Δ年化 +3.0、Δ回撤 +4.0
    weak = [dict(d) for d in deltas]
    for d in weak:
        if d["cell"] == "20%x2":
            d["delta_dd_improve_pp"] = 0.5                    # 年化够、回撤不够
        if d["cell"] == "20%x3":
            d["delta_annual_pp"] = 0.2                        # 回撤够、年化不够
    assert mod.e33_decision(weak)["verdict"] == "不过"
    for d in weak:
        if d["cell"] == "20%x3":
            d["delta_annual_pp"] = 2.0                        # 补回来 ⇒「至少一格」即可
    assert mod.e33_decision(weak)["verdict"] == "过"


def test_e33_cannot_be_satisfied_by_the_10_or_30_pair():
    deltas, _ = mod.pair_deltas(ALL_ROWS)
    verdict = mod.e33_decision([d for d in deltas if d["reserve_level"] != 20])
    assert verdict["verdict"] == "无法判定"
    assert "C20" in verdict["reason"]


# ---- E3-4：单调性用 Δ 序列，不用原始指标 ----
def _axis(annual_by_level, dd_by_level, tranches=2):
    return [{"cell": f"{lvl}%x{tranches}", "control": f"C{lvl}", "reserve_level": lvl,
             "tranches": tranches, "delta_annual_pp": annual_by_level[lvl],
             "delta_dd_improve_pp": dd_by_level[lvl]} for lvl in (10, 20, 30)]


def test_e34_judges_the_delta_axis_even_when_raw_levels_disagree():
    """原始年化在 30% 档最高（留的现金最多、 mechanically 少亏），Δ 序列却是 20% 为峰 ⇒ 判过。

    这就是为什么单调性必须用 Δ：原始值的大小顺序被「少投了多少钱」带着走。
    """
    deltas = _axis({10: 1.0, 20: 3.0, 30: 0.5}, {10: 1.0, 20: 2.0, 30: 1.5})
    out = mod.e34_decision(deltas, [], [])
    assert out["axes"]["tranches=2"]["status"] == "过"
    raw = {10: -8.0, 20: -5.0, 30: -4.0}
    assert sorted(raw, key=raw.get, reverse=True)[0] == 30      # 原始口径会把峰判成 30%
    assert max(deltas, key=lambda d: d["delta_annual_pp"])["reserve_level"] == 20


def test_e34_fails_when_20_is_not_the_peak_of_the_delta_axis():
    deltas = _axis({10: 1.0, 20: 3.0, 30: 6.0}, {10: 1.0, 20: 2.0, 30: 1.5})
    axis = mod.e34_decision(deltas, [], [])["axes"]["tranches=2"]
    assert axis["status"] == "不过"
    assert not axis["checks"]["Δ年化"]["ok"]
    assert axis["checks"]["Δ回撤改善"]["ok"]                     # 另一项成立也照样判不过


def test_e34_cannot_judge_an_axis_when_a_level_has_no_control():
    """少一个同档对照（比如没跑 C30）⇒ Δ 序列凑不齐三档，这是「无法判定」不是「过」。"""
    deltas, unpaired = mod.pair_deltas(rows_without("C30"))
    assert sorted(unpaired) == ["30%x2", "30%x3"]
    out = mod.e34_decision(deltas, unpaired, [])
    assert out["axes"]["tranches=2"]["status"] == "无法判定"
    assert out["verdict"] == "不过"


def test_e34_passes_on_the_full_preregistered_grid():
    deltas, unpaired = mod.pair_deltas(ALL_ROWS)
    out = mod.e34_decision(deltas, unpaired,
                           [{"mechanism": {"next_day_rel_pp": 1.0}}] * 6)
    assert out["verdict"] == "过", out["axes"]
    assert out["axes"]["tranches=3"]["checks"]["Δ回撤改善"] == \
        {"10%": 2.0, "20%": 2.0, "30%": -2.0, "ok": True}


def test_e34_requires_the_mechanism_column_to_agree_in_sign():
    verdicts = [{"mechanism": {"next_day_rel_pp": pp}} for pp in
                [1.0, 1.0, 1.0, 1.0, 1.0, -0.5]]
    assert mod.e34_decision([], [], verdicts)["mechanism"]["ok"] is True      # 5/6 同号
    verdicts = [{"mechanism": {"next_day_rel_pp": pp}} for pp in
                [1.0, 1.0, 1.0, -1.0, -1.0, -1.0]]
    assert mod.e34_decision([], [], verdicts)["mechanism"]["ok"] is False     # 3/3 打平


def test_e34_ignores_nan_mechanism_cells():
    verdicts = [{"mechanism": {"next_day_rel_pp": pp}} for pp in
                [1.0, 1.0, 1.0, 1.0, 1.0, float("nan")]]
    verdicts.append({"mechanism": None})
    out = mod.e34_decision([], [], verdicts)["mechanism"]
    assert out["n_cells"] == 5 and out["positive"] == 5
    assert out["required"] == "同号≥5/6"


# ---- 三条并判 ----
def test_delta_passing_but_monotonicity_failing_is_not_trusted():
    final = mod.final_decision({"verdict": "过", "reason": "x"}, {"verdict": "不过"}, 6)
    assert final["verdict"] == "不采信"
    assert "偶然命中" in final["reason"]


def test_gates_alone_never_make_the_call():
    final = mod.final_decision({"verdict": "不过", "reason": "Δ 不达标"}, {"verdict": "过"}, 6)
    assert final["verdict"] == "不采信"
    assert "E3-3" in final["reason"]
    assert "关掉" in final["e8_action"]


def test_both_sufficiency_checks_passing_is_trusted():
    final = mod.final_decision({"verdict": "过", "reason": "x"}, {"verdict": "过"}, 6)
    assert final["verdict"] == "采信"
    assert "新预注册" in final["e8_action"]


# ---- 台账（E3-7：账本级的错最不容易被自己发现）----
def test_ledger_n_pass_equals_the_number_of_cells_judged_pass():
    rows = [{"cell": r["cell"]} for r in ALL_ROWS[1:]]           # 9 格，基准不进台账
    verdicts = [{"verdict": v} for v in
                ["过", "过", "过", "过", "过", "不过", "对照", "对照", "对照"]]
    rec = mod.ledger_record(len(rows), rows, verdicts, mod.LEDGER_EXPECT_BEFORE, mod.HOLDOUT)
    assert rec["n_pass"] == sum(1 for v in verdicts if v["verdict"] == "过") == 5
    assert rec["combos"] == 9                        # 6 实验格 + 3 对照格
    assert rec["trials_total"] == 129_913
    assert rec["batch"] == "E3"
    assert rec["cells"] == ["10%x2", "20%x2", "30%x2", "10%x3", "20%x3", "30%x3",
                            "C10", "C20", "C30"]
    assert rec["window"] == ["2022-01-01", "2026-09-30"]


def test_ledger_n_pass_ignores_placeholders_and_controls():
    verdicts = [{}, {"verdict": "过"}, {"verdict": "不过"}, {"verdict": "过"},
                {"verdict": "对照"}, {"verdict": "过"}]
    rows = [{"cell": str(i)} for i in range(6)]
    rec = mod.ledger_record(6, rows, verdicts, 0, mod.HOLDOUT)
    assert rec["n_pass"] == 3
    assert rec["trials_total"] == 6


def test_judge_skips_the_controls_and_the_baseline():
    """对照格按 E3-2 不参与 ①–⑥：判定表里它是「对照」，不会被算进 n_pass。"""
    baseline = {k: v for k, v in BY_NAME["基准"].items()}
    assert mod.judge(baseline, baseline, {}) == {}
    for name in ("C10", "C20", "C30"):
        v = mod.judge(BY_NAME[name], baseline, {})
        assert v["verdict"] == "对照" and v["gates"] == {}


# ---- C30 这条通道：预留 30%、tranches=0、永不放松、机制列不适用 ----
def test_c30_holds_its_reserve_on_every_day_including_crash_days():
    from stock_selector.backtest.cash_policy import CashPolicy
    days = ["2022-03-10", "2022-03-11", "2022-03-14", "2022-03-15"]
    crashes = {"2022-03-14", "2022-03-15"}           # 锚点连着两天，日程本来会放开
    p = CashPolicy(reserve_pct=0.30, tranches=0, triggers=crashes, days=days)
    assert [p.floor(d, 100_000) for d in days] == [30_000.0] * 4
    assert p.active_days == []                        # 没放过子弹 ⇒ 机制读数无从谈起
    # 同参数的实验格 30%x2 会在锚点日放松，两者唯一的差别就是这条通道
    exp = CashPolicy(reserve_pct=0.30, tranches=2, triggers=crashes, days=days)
    assert [exp.floor(d, 100_000) for d in days] == [30_000.0, 30_000.0, 15_000.0, 0.0]
    assert exp.active_days == ["2022-03-14", "2022-03-15"]


# ---- 窗与矩阵严格隔离（硬规则 1）----
def test_holdout_window_never_points_at_the_e2_artifacts():
    assert mod.HOLDOUT.matrix_dir == mod.ROOT / "data" / "portfolio_holdout"
    assert mod.HOLDOUT.market_state == mod.ROOT / "data" / "actions_holdout" / "market_state.parquet"
    assert mod.HOLDOUT.date_from == "2022-01-01" and mod.HOLDOUT.date_to == "2026-09-30"
    assert mod.HISTORY.matrix_dir != mod.HOLDOUT.matrix_dir
    assert mod.HISTORY.market_state != mod.HOLDOUT.market_state
