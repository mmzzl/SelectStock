"""因子闸门口径测试：分位取值、缺值放行、三臂达标判定、配对差值。

闸门是整条路线的最后一道闸，所以这几件事必须钉死：
- 分位是**当日样本内**的横截面百分位（跨时代可比），不是全时代分位也不是绝对值；
- 取不到分位按**放行**（与实盘「信息不够就不判」一致）——改成剔会让闸门在稀疏票上
  悄悄砍掉一大截，而原因不在判据；
- 达标判定要四项同时（期望>0、赔率、胜率、利润因子），单过一项不算。
"""

import importlib.util
import sys

import pandas as pd
import pytest

SCRIPT = "scripts/backtest_factor_gate.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("backtest_factor_gate", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["backtest_factor_gate"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load_script()


@pytest.fixture(autouse=True)
def _clear_rank_cache():
    mod._RANKS.clear()
    yield
    mod._RANKS.clear()


def _write(dirpath, code, rows):
    df = pd.DataFrame(rows)
    df["code"] = code
    df["date"] = pd.to_datetime(df["date"])
    df.to_parquet(dirpath / f"{code}.parquet", index=False)


@pytest.fixture
def gate_dir(tmp_path, monkeypatch):
    """三只票：同一天 roc10 分别 1.0 / 5.0 / 9.0 → 当日分位 33.3 / 66.7 / 100。"""
    rows = lambda v: [{"date": "2026-09-01", "roc10": v},
                      {"date": "2026-09-02", "roc10": v}]
    _write(tmp_path, "000001", rows(1.0))
    _write(tmp_path, "000002", rows(5.0))
    _write(tmp_path, "000003", rows(9.0))
    monkeypatch.setattr(mod, "KLINE_DIR", tmp_path)
    return tmp_path


def test_gate_uses_within_day_cross_section_rank(gate_dir):
    gate = mod.FactorGate("2026-09-01", "2026-09-02", ("000001", "000002", "000003"),
                          [{"factor": "roc10", "bin": 4}])
    assert gate("2026-09-01", "000003") is True     # 分位 100 → 落在 [80,100)
    assert gate("2026-09-01", "000002") is False    # 分位 66.7
    assert gate("2026-09-02", "000003") is True     # 换一天重算，仍是当日最高


def test_gate_passes_when_rank_unavailable(gate_dir):
    gate = mod.FactorGate("2026-09-01", "2026-09-02", ("000001", "000002", "000003"),
                          [{"factor": "roc10", "bin": 4}])
    assert gate("2026-09-01", "000002") is False    # 先确认它平时会被剔
    assert gate("2026-09-03", "000002") is True     # 这天没数据 → 不判 → 放行
    assert gate("2026-09-01", "999999") is True     # 这只票没行 → 放行


def test_gate_modes(gate_dir):
    universe = ("000001", "000002", "000003")
    rules = [{"factor": "roc10", "bin": 0}, {"factor": "roc10", "bin": 4}]
    assert mod.FactorGate("2026-09-01", "2026-09-02", universe, rules, "union")(
        "2026-09-01", "000003") is True             # 任一命中即放行
    assert mod.FactorGate("2026-09-01", "2026-09-02", universe, rules, "all")(
        "2026-09-01", "000003") is False            # 要求两条都命中（000003 只中 bin4）


def _cell(era, offset, n, wr, payoff, pf, exp):
    return (era, offset, {"n": n, "wr": wr, "payoff": payoff, "pf": pf, "exp": exp,
                          "ret": 0.0, "bh": 0.0, "dd": 0.0, "rej": 0, "expo": 0.2})


class Args:
    min_trades = 30
    payoff_target = 2.0
    wr_target = 0.50
    pf_target = 2.0
    n_offsets = 3


def test_summary_pass_requires_all_four_targets():
    good = [_cell("2019", 0, 40, 0.55, 2.2, 2.5, 100.0)] * 3
    assert mod._summarize("s", (0.05, 0.0, 10), "对照(无门)", good, [], Args())["pass"] is True
    # 只把胜率压到 50% 以下：赔率与利润因子都还过线，但整族不算达标
    low_wr = [_cell("2019", 0, 40, 0.49, 2.4, 2.6, 100.0)] * 3
    rec = mod._summarize("s", (0.05, 0.0, 10), "对照(无门)", low_wr, [], Args())
    assert rec["pass"] is False and rec["payoff_min"] >= 2.0 and rec["pf_min"] >= 2.0
    # 笔数不到下限也不算
    thin = [_cell("2019", 0, 29, 0.60, 2.4, 2.6, 100.0)] * 3
    assert mod._summarize("s", (0.05, 0.0, 10), "对照(无门)", thin, [], Args())["pass"] is False
    # 期望为负同样不算（赔率与利润因子高但期望负 = 截断亏损凑出来的）
    neg = [_cell("2019", 0, 40, 0.60, 2.4, 2.6, -10.0)] * 3
    assert mod._summarize("s", (0.05, 0.0, 10), "对照(无门)", neg, [], Args())["pass"] is False


def test_paired_deltas_are_within_cell_and_flag_flip():
    ctl = [_cell("2019", 0, 40, 0.45, 1.8, 1.5, 100.0),
           _cell("2022", 0, 40, 0.45, 1.8, 1.5, 100.0),
           _cell("2025", 0, 40, 0.45, 1.8, 1.5, 100.0)]
    better = [_cell("2019", 0, 20, 0.55, 2.2, 2.5, 150.0),
              _cell("2022", 0, 20, 0.55, 2.2, 2.5, 150.0),
              _cell("2025", 0, 20, 0.55, 2.2, 2.5, 150.0)]
    rec = mod._summarize("s", (0.05, 0.0, 10), "roc10[80,100)", better, ctl, Args())
    assert rec["exp_stable"] and rec["wr_stable"] and rec["payoff_stable"]
    assert rec["d_exp_mean"] == pytest.approx(50.0)
    assert rec["d_wr_mean"] == pytest.approx(0.10)

    flipped = [better[0], better[1], _cell("2025", 0, 20, 0.30, 1.5, 1.2, 50.0)]
    rec = mod._summarize("s", (0.05, 0.0, 10), "roc10[80,100)", flipped, ctl, Args())
    assert rec["exp_stable"] is False        # 有一格反号 → 当噪声


def test_parse_rules_validates_format():
    assert mod.parse_rules("adx14:4") == [{"factor": "adx14", "bin": 4, "why": "手工指定"}]
    assert [r["factor"] for r in mod.parse_rules("adx14:4, pct:0 ")] == ["adx14", "pct"]
    assert mod.parse_rules("adx14:>=90")[0]["min_pct"] == 90.0
    for bad in ("adx14", "adx14:9", "adx14:x", "adx14:>=0", "adx14:>=101", ""):
        with pytest.raises(SystemExit):
            mod.parse_rules(bad)


def test_gate_min_pct_is_inclusive_at_the_boundary(gate_dir):
    """`>=百分位` 闭区间：分位正好等于门槛的票必须放行，否则 90 与 91 差一整个样本。"""
    gate = mod.FactorGate("2026-09-01", "2026-09-02", ("000001", "000002", "000003"),
                          [{"factor": "roc10", "min_pct": 100.0}])
    assert gate("2026-09-01", "000003") is True     # 分位 100.0 ≥ 100
    assert gate("2026-09-01", "000002") is False    # 分位 66.7
    gate90 = mod.FactorGate("2026-09-01", "2026-09-02", ("000001", "000002", "000003"),
                            [{"factor": "roc10", "min_pct": 90.0}])
    assert gate90("2026-09-01", "000003") is True
