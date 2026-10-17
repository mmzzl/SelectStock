"""合取判据预注册批次（B）的口径测试。

这批是「单条件扫到头 → 换成两根轴的合取」的第一批，价值全在两条纪律上：
1. **阈值一个都不许新增** —— 60 条必须由 v1 已注册的 12 条尾档两两 AND 而成，
   自己挑分位点等于把多重比较的账偷偷抹掉；
2. **批次必须整批跑** —— `--limit-conditions` 从 60 条里挑几条看，挑的动作就回到了数据里。
所以这里逐条钉住来源、条数、文法和拒抽稀的行为。
"""
import importlib.util
import itertools
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BATCH = ROOT / "config/rule_candidates_conj.json"
INVENTORY = ROOT / "logs/profit_scan/base_conditions.json"


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, str(ROOT / rel))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cc():
    sys.path.insert(0, str(ROOT))
    return _load("conjunction_candidates", "scripts/conjunction_candidates.py")


@pytest.fixture(scope="module")
def ps():
    return _load("profit_scan", "scripts/profit_scan.py")


@pytest.fixture(scope="module")
def batch():
    assert BATCH.is_file(), "先跑 scripts/conjunction_candidates.py 生成批次文件"
    return json.loads(BATCH.read_text(encoding="utf-8"))


def test_batch_declares_and_contains_60(batch):
    assert batch["n_conditions"] == len(batch["conditions"]) == 60
    assert batch["n_axis_pairs"] == batch["n_axes"] * (batch["n_axes"] - 1) // 2 == 15
    assert batch["n_conditions"] == batch["n_axis_pairs"] * 4
    assert batch["n_cells"] == 60 * batch["n_profiles"] == 3840
    assert batch["holdout_start"] == "2021-12-31"
    assert len({c["id"] for c in batch["conditions"]}) == 60


def test_every_threshold_is_reused_from_registered_tails(batch, cc):
    """12 条源档 = 6 根轴的两端，且每条合取的 expr 正好是两个源 expr 的 AND。"""
    src_ids = {ax[t]["id"] for ax in cc.AXES.values() for t in ("L", "H")}
    assert len(src_ids) == 12
    inv = {c["id"]: c for c in json.loads(INVENTORY.read_text(encoding="utf-8"))["conditions"]}
    seen = set()
    for c in batch["conditions"]:
        ids = [s["id"] for s in c["sources"]]
        assert len(ids) == 2 and ids[0] != ids[1]
        for i in ids:
            assert i in src_ids, f"{c['id']} 用了没注册过的档 {i}"
            seen.add(i)
        assert c["expr"] == f"{inv[ids[0]]['expr']} and {inv[ids[1]]['expr']}"
    assert seen == src_ids, "有源档一次都没被用到，说明轴对少配了"


def test_quadrants_cover_all_axis_pairs(batch, cc):
    want = {f"c_{a}{x}_{b}{y}" for (a, b) in cc.WHY
            for x, y in itertools.product("LH", "LH")}
    assert {c["id"] for c in batch["conditions"]} == want
    for c in batch["conditions"]:
        assert c["kind"] == "conjunction" and c["why"].strip()
        assert c["percentile"] is None            # 合取没有分位档，别造一个


def test_exprs_pass_the_live_grammar(batch):
    """expr 文法与线上判定同一真源；字段全在白名单里，扫描才不会跑到一半炸。"""
    from stock_selector.backtest import rule_expr

    for c in batch["conditions"]:
        fields = rule_expr.validate(c["expr"])
        assert len(fields) >= 2
        assert set(fields) == set(c["fields"])


def test_vectorized_and_matches_hand_computation(batch):
    """合取在网格式求值下必须等于「两个尾档各自成立再 AND」，NaN 行不触发。

    这是扫描实际走的那条路径（`rule_expr.eval_matrix`），不是逐行 eval。
    """
    import numpy as np
    from stock_selector.backtest import rule_expr

    rng = np.random.default_rng(7)
    n = 200
    cols = {f: rng.uniform(0, 2, n) for f in ("pos60", "vol_ratio", "vol_z", "rsi14", "adx14",
                                              "close", "ma20")}
    for f in cols:
        cols[f][:20] = np.nan                         # 全缺数：两头都不许算触发
    cols["pos60"][20:40] = np.nan                     # 只缺 pos60：引用它的条件必须整块不触发
    for c in batch["conditions"]:
        got = rule_expr.eval_matrix(c["expr"], cols)
        want = (rule_expr.eval_matrix(c["sources"][0]["expr"], cols)
                & rule_expr.eval_matrix(c["sources"][1]["expr"], cols))
        assert np.array_equal(got, want), c["id"]
        assert not got[:20].any(), c["id"]
        if "pos60" in c["fields"]:
            assert not got[20:40].any(), c["id"]


def test_generator_is_deterministic(cc, batch):
    """重跑生成器必须得到同一批 id/expr：预注册的意义就是这条清单不随读数变。"""
    conds = cc.build(cc.load_sources(INVENTORY))
    assert [c["id"] for c in conds] == [c["id"] for c in batch["conditions"]]
    assert [c["expr"] for c in conds] == [c["expr"] for c in batch["conditions"]]
    assert len(conds) == 60


def test_load_inventory_reads_the_batch_whole(ps, batch):
    conds, info = ps.load_inventory(0, str(BATCH))
    assert len(conds) == 60
    assert info["batch_id"] == batch["batch_id"]
    assert {"id", "kind", "field", "ref", "expr", "label", "percentile",
            "sample_trigger_pct"} == set(conds[0])
    assert all(c["kind"] == "conjunction" for c in conds)


def test_subsample_of_a_preregistered_batch_is_refused(ps):
    with pytest.raises(SystemExit, match="整批跑"):
        ps.load_inventory(10, str(BATCH))


def test_tampered_batch_is_refused(ps, tmp_path):
    doc = json.loads(BATCH.read_text(encoding="utf-8"))
    doc["conditions"].pop()                                       # 偷偷少一条
    p = tmp_path / "tampered.json"
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SystemExit, match="条数不许改"):
        ps.load_inventory(0, str(p))

    doc = json.loads(BATCH.read_text(encoding="utf-8"))
    doc["conditions"] = []                                        # 空批次
    p2 = tmp_path / "empty.json"
    p2.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SystemExit, match="没有 conditions"):
        ps.load_inventory(0, str(p2))

    with pytest.raises(SystemExit, match="找不到文件"):
        ps.load_inventory(0, str(tmp_path / "nope.json"))
