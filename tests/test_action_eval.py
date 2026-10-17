"""动作模型验收端（批次 C 的下游）与批次 D 两臂的口径测试。

这里钉的是「12 格（批 D 是 24 格）的读数是怎么算出来的」，三件最容易自欺的事：
1. **分位阈值按名次取**：模型没有信号时预测会退化成一条常数（早停直接压平），
   这时 `p <= np.quantile(p, 0.3)` 会选中 **全部** 行——三格变成同一格，读数说的却是「最挑的 30%」；
2. **同一仓位只算第一次触发**：真实仓位卖一次就没了，一波里连着 6 天都想卖不能记 6 笔；
3. **判定只看按交易日聚类的 t**：同一天几千只票一起涨一起跌，逐笔 t 会把「当天买什么都赚」
   算成自己的本事（v1/B 那批 3,840 格全部卡在这一道上）。

批次 D 加的四条：两臂的**特征差集必须恰好是那 9 列**（否则归因不成立）、换标签**不许动判定**、
超额标签只减当期中位数（日内名次不变）、横截面连不上**留 NaN 不丢行**。
"""
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, str(ROOT / rel))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def am():
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "scripts"))
    return _load("train_action_model", "scripts/train_action_model.py")


@pytest.fixture(scope="module")
def bs():
    return _load("build_action_samples2", "scripts/build_action_samples.py")


def test_rank_mask_on_a_degenerate_constant_prediction(am):
    """预测全是同一个数时，名次法仍然恰好取 k 行，而不是「全都算触发」。"""
    p = np.zeros(1000)
    elig = np.ones(1000, dtype=bool)
    for pct, k in ((30, 300), (20, 200), (10, 100)):
        m, cut = am.rank_mask(p, elig, pct / 100.0, lowest=True)
        assert int(m.sum()) == k, f"{pct}% 应该恰好 {k} 行，实际 {int(m.sum())} 行"
        assert cut == 0.0


def test_rank_mask_takes_the_extremes_on_the_right_side(am):
    p = np.arange(100, dtype=float)
    elig = np.ones(100, dtype=bool)
    lo, lo_cut = am.rank_mask(p, elig, 0.10, lowest=True)
    hi, hi_cut = am.rank_mask(p, elig, 0.10, lowest=False)
    assert sorted(np.flatnonzero(lo).tolist()) == list(range(10))
    assert sorted(np.flatnonzero(hi).tolist()) == list(range(90, 100))
    assert lo_cut == 9.0 and hi_cut == 90.0


def test_rank_mask_only_counts_eligible_rows_when_choosing_k(am):
    """加仓要买得进：k 按「可动的行」算，不可动的行也永远不会被选中。"""
    p = np.arange(100, dtype=float)
    elig = np.zeros(100, dtype=bool)
    elig[50:] = True                        # 前一半买不进
    m, cut = am.rank_mask(p, elig, 0.10, lowest=True)
    assert int(m.sum()) == 5
    assert not m[:50].any()
    assert cut == 54.0


def test_first_act_per_episode_keeps_the_earliest_trigger(am):
    """同一仓位只算第一次触发（样本按 (代码, episode, h) 有序，所以就是最小 h 那行）。"""
    ep_key = np.array([7, 7, 7, 3, 3, 9, 9, 9])
    acted = np.array([False, True, True, True, True, False, False, True])
    keep = am.first_act_per_episode(acted, ep_key)
    assert sorted(keep.tolist()) == [1, 3, 7]        # 每个 episode 只剩第一条
    assert am.first_act_per_episode(np.zeros(8, dtype=bool), ep_key).size == 0


def test_day_stat_uses_day_clustered_t(am):
    """同一天内的笔高度相关：按日聚类的 t 必须比逐笔 t 小，否则显著性是虚的。"""
    adv = np.array([2.0, 4.0, 6.0, 8.0])
    d_idx = np.array([0, 0, 1, 1])
    st = am.day_stat(adv, d_idx, 2, (0, 1))
    assert st["n"] == 4 and st["days"] == 2
    assert st["mean"] == pytest.approx(5.0)
    assert st["day_mean"] == pytest.approx(5.0)
    assert st["t_day"] == pytest.approx(2.5)         # 只有 2 天 ⇒ 证据只有 2 个独立观测
    assert st["t_trade"] == pytest.approx(3.872983, abs=1e-4)
    assert st["win"] == 1.0 and np.isinf(st["payoff"])   # 一笔不亏 → ∞，不是缺数


def test_day_stat_win_and_payoff(am):
    st = am.day_stat(np.array([1.0, -1.0, 2.0, -2.0]), np.arange(4), 4, (1, 2))
    assert st["win"] == pytest.approx(0.5)
    assert st["payoff"] == pytest.approx(1.0)


def test_day_benchmark_is_the_unselected_daily_average(am):
    """同日基准 = 那天「所有可判持仓（不挑）」的平均，不是全样本平均。"""
    adv = np.array([10.0, -6.0, 3.0, 1.0])
    d_idx = np.array([0, 0, 1, 1])
    elig = np.array([True, True, True, False])
    bench = am.day_benchmark(adv, d_idx, elig, 2)
    assert bench[0] == pytest.approx(2.0)            # (10-6)/2
    assert bench[1] == pytest.approx(3.0)            # 只算可动的第 2 行


def test_evaluate_cells_perfect_foresight_is_profitable(am):
    """给一个「全知的模型」（预测=真实前向收益）：卖侧挑 r 最低的应该赚钱，
    加侧挑 r 最高的也应该赚钱。这条挂了说明优势公式（费用、方向、符号）写反了。"""
    n = 4000
    rng = np.random.default_rng(7)
    r = rng.uniform(-0.06, 0.06, n)
    day = rng.integers(0, 120, n)
    data = {"day": day, "ep_key": np.arange(n), "buyable": np.ones(n, dtype=np.int8),
            "r": {5: r, 10: r}}
    rows, need_t = am.evaluate_cells(data, {5: r.copy(), 10: r.copy()}, 12)
    assert len(rows) == 2 * 2 * len(am.CUTOFFS) == 12
    assert need_t == pytest.approx(am.rule_gates.t_required(12), abs=1e-4)
    sells = [x for x in rows if x["side"] == "sell"]
    adds = [x for x in rows if x["side"] == "add"]
    assert all(x["net_per_trade_pp"] > 0 and x["win_rate"] > 0.5 for x in sells)
    assert all(x["net_per_trade_pp"] > 0 and x["win_rate"] > 0.5 for x in adds)
    # 卖侧 = 少拿那段跌幅，扣的是卖出单边费；加侧 = 多拿一段涨幅，扣一轮换手
    s10 = [x for x in sells if x["cutoff_pct"] == 10][0]
    want = float(np.mean(-r[np.argsort(r, kind="stable")[:400]] * (1.0 - am.SELL_FEE_RATE) * 100))
    assert s10["net_per_trade_pp"] == pytest.approx(want, abs=1e-3)
    a10 = [x for x in adds if x["cutoff_pct"] == 10][0]
    want_add = float(np.mean(r[np.argsort(r, kind="stable")[-400:]] * 100) - am.ROUND_TRIP_PP)
    assert a10["net_per_trade_pp"] == pytest.approx(want_add, abs=1e-3)
    # 每格动手的比例就是它标称的那个分位（30/20/10），不多不少
    assert all(x["act_share_pct"] == pytest.approx(x["cutoff_pct"]) for x in sells + adds)


def test_evaluate_cells_uses_cumulative_trials_for_the_gate(am):
    """及格线按「判这批格时一共试过多少次」抬：试得越多线越高；同一批预注册格复跑
    （只改展示、不加新格）由调用方传台账里已有的累计数，不该再抬一次。"""
    from stock_selector.backtest import rule_gates
    n = 6000                       # 10% 那格也有 600 笔，笔数门槛先过，才轮得到 sig 那道
    r = np.linspace(-0.05, 0.05, n)
    data = {"day": np.arange(n) % 100, "ep_key": np.arange(n),
            "buyable": np.ones(n, dtype=np.int8), "r": {5: r}}
    rows, need_t = am.evaluate_cells(data, {5: r.copy()}, 129_862)
    assert len(rows) == 6
    want_t = rule_gates.t_required(129_862)
    assert need_t == pytest.approx(want_t, abs=1e-6)
    for x in rows:
        last = x["checks"][-1]
        assert last["key"] == "sig", "最后一道才是防自欺的试次门槛，前面不能先被卡掉"
        assert last["need"] == f"试了 {129_862:,} 次，要 t≥{want_t:.2f}"
        if x["t_day"] is not None and x["t_day"] < want_t:
            assert "sig" in x["failed"]
    # 试次少了线就低：同一份读数在 12 次试次下能过，在 129,862 次下过不了
    rows_small, need_small = am.evaluate_cells(data, {5: r.copy()}, 12)
    assert need_small < need_t
    assert any(x["verdict"] == "过" for x in rows_small)


def test_era_cuts_follow_the_judged_days(am):
    """三段按「真有样本外读数的日子」切，不按样本表的日期范围切：验证折的起点晚于样本起点时，
    按全范围切会让第一段永远是空的，报告上就看不出「这个动作只在前一段赚」。"""
    n = 6000
    r = np.linspace(-0.05, 0.05, n)
    day = np.arange(n) % 300
    pred = r.copy()
    pred[day < 120] = np.nan                   # 前 120 天没有样本外预测（训练行不够，折被跳过）
    data = {"day": day, "ep_key": np.arange(n), "buyable": np.ones(n, dtype=np.int8), "r": {5: r}}
    rows, _ = am.evaluate_cells(data, {5: pred}, 12)
    seg = [x for x in rows if x["side"] == "sell" and x["cutoff_pct"] == 30][0]
    assert "nan" not in seg["era_net_pp"], seg["era_net_pp"]
    assert len(seg["era_net_pp"].split(",")) == 3


def test_trainer_features_exist_in_the_sample_table(bs, am):
    """列契约：训练侧要的每一个特征/标签都得是建表脚本产出的列，改名要在这先炸。"""
    cols = set(bs.SCHEMA_NAMES)
    assert set(am.NUMERIC_FEATURES) <= cols, set(am.NUMERIC_FEATURES) - cols
    assert set(am.REQUIRED) <= cols
    assert {"date", "code", "ep_idx", "buyable"} <= cols
    assert {"board", "r5", "r10"} <= cols
    assert len(am.CUTOFFS) == 3


# ---------------------------------------------------------------- 批次 D：两臂 + 新输入


@pytest.fixture(scope="module")
def xc():
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "scripts"))
    return _load("build_cross_section2", "scripts/build_cross_section.py")


def test_d_feature_names_match_what_the_builder_writes(am, xc):
    """列契约（D）：trainer 要的 9 列必须逐名等于 build_cross_section 写出的列，`mkt_n` 不许回来。"""
    want = set(am.XS_FEATURES) | set(am.MKT_FEATURES)
    assert want == set(xc.XS_COLUMNS) | (set(xc.MKT_COLUMNS) - {"mkt_n"})
    assert am.D_FEATURES == xc.XS_COLUMNS + xc.MKT_COLUMNS[:-1]
    assert "mkt_n" not in am.D_FEATURES


def test_features_for_only_adds_columns_for_d2(am):
    """c 与 d1 的特征必须逐项相同——两臂的差别只许在标签上，否则读数没法归因。"""
    assert am.features_for("c") == am.NUMERIC_FEATURES + ["board"]
    assert am.features_for("d1") == am.features_for("c")
    assert am.features_for("d2") == am.NUMERIC_FEATURES + am.D_FEATURES + ["board"]


def test_matrix_from_follows_the_requested_column_order(am):
    cols = {"a": np.array([1.0, 2.0], dtype=np.float32),
            "b": np.array([3.0, 4.0], dtype=np.float32),
            "board": np.array([5.0, 6.0], dtype=np.float32)}
    x = am.matrix_from(cols, ["board", "b", "a"])
    assert x.dtype == np.float32
    assert x.tolist() == [[5.0, 3.0, 1.0], [6.0, 4.0, 2.0]]


def test_excess_label_subtracts_only_the_same_day_median(am):
    """`y = r − 当日中位数`：同一天内整体平移 ⇒ 名次不变（模型学的还是「谁的下一段比同场持仓好」），
    跨日的差别才被抹掉。标签缺值的行必须还是 NaN，不许当成 0 喂进树。"""
    day = np.array([0, 0, 0, 0, 1, 1, 1, 1], dtype=np.int32)
    r = np.array([-0.10, -0.03, 0.01, 0.07, -0.02, 0.00, 0.05, np.nan])
    y, info = am.excess_labels(day, {5: r})
    med0 = np.median(r[:4])                       # 第 1 天四行：(-0.03 + 0.01)/2
    assert y[5][:4] == pytest.approx(r[:4] - med0)
    assert np.argsort(y[5][:4]).tolist() == np.argsort(r[:4]).tolist()
    assert y[5][4:7] == pytest.approx(r[4:7])     # 第 2 天中位数 0.00
    assert np.isnan(y[5][7]), "r 缺值的行不能被中位数变成有意义的数"
    assert info["5"]["usable_rows"] == 7


def test_evaluate_cells_reads_the_same_ruler_for_every_arm(am):
    """换标签不许动判定：同一份预测、同一份真实 `r`，臂 c 与臂 d1 的每一格读数必须逐字段相同。"""
    n = 2000
    rng = np.random.default_rng(3)
    r = rng.uniform(-0.05, 0.05, n)
    data = {"day": rng.integers(0, 60, n), "ep_key": np.arange(n),
            "buyable": np.ones(n, dtype=np.int8), "r": {5: r}}
    pred = {5: r.copy()}
    c, t_c = am.evaluate_cells(data, pred, 400, arm="c")
    d1, t_d1 = am.evaluate_cells(data, pred, 400, arm="d1")
    assert len(c) == len(d1) == 6
    assert t_c == t_d1
    for a, b in zip(c, d1):
        assert a["arm"] == "c" and b["arm"] == "d1"
        assert {k: v for k, v in a.items() if k != "arm"} == {k: v for k, v in b.items() if k != "arm"}


def test_join_cross_section_keeps_every_row_and_leaves_nan(am, tmp_path):
    """连不上的决策行必须留 NaN、不丢行：丢行等于用「特征可用性」重新定义样本（README 批次 D）。"""
    import pandas as pd

    d1, d2 = pd.Timestamp("2020-01-01"), pd.Timestamp("2020-01-02")
    pd.DataFrame({"date": [d1, d1, d2], "code": ["600000", "000001", "600000"],
                  **{c: [0.1, 0.2, 0.3] for c in am.XS_FEATURES}}
                 ).to_parquet(tmp_path / "cross_section.parquet", index=False)
    pd.DataFrame({"date": [d1, d2], **{c: [10.0, 20.0] for c in am.MKT_FEATURES}}
                 ).to_parquet(tmp_path / "market_state.parquet", index=False)
    day = np.array([18262, 18262, 18263, 18263], dtype=np.int32)   # 2020-01-01 / -02 的纪元日序号
    code_id = np.array([0, 1, 0, 1], dtype=np.int32)               # 第 4 行 (02, 000001) 表里没有
    out, cov = am.join_cross_section(day, code_id, {"600000": 0, "000001": 1},
                                     am.D_FEATURES, tmp_path)
    assert set(out) == set(am.D_FEATURES)
    assert len(out["xs_bias20"]) == 4
    assert out["xs_bias20"][:3] == pytest.approx([0.1, 0.2, 0.3])
    assert np.isnan(out["xs_bias20"][3])
    assert out["mkt_up_ratio"].tolist() == [10.0, 10.0, 20.0, 20.0], "市场列按日查，不该跟着票一起缺"
    assert cov["xs_bias20"] == 75.0 and cov["mkt_up_ratio"] == 100.0
    only_mkt, _ = am.join_cross_section(day, code_id, {"600000": 0, "000001": 1},
                                        ["mkt_vol20"], tmp_path)
    assert list(only_mkt) == ["mkt_vol20"], "只要一列时不该把横截面表整个读进来"

