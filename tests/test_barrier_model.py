"""成本锚 barrier 臂（§50）的脚本级口径测试：标签合成、分组完整、单调约束、分层校准。

训一次要几十分钟、要吃 10.33M 行样本，所以这里锁的是**决定读数对不对的那几件纯函数**：
- 标签：`y = 1(fwd_low <= close*(1-x))`——每行配 K 个 barrier，x 是「还要跌多少才碰到线」；
- 单调约束只加在 barrier 列（其他特征不许被约束，等于让模型「只能一个方向回答」）；
- 校准按 barrier 分层各出一条 isotonic 曲线（现臂 10 档阶梯就是 600272 连续 26 天印同一个数的成因）；
- 折划分不许把同一组复制行拆到训练/验证两侧（同一根 fwd_low 两头出现，AUC 直接虚高）。
"""
import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "train_barrier_model.py"


def _load():
    spec = importlib.util.spec_from_file_location("train_barrier_model", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load()


def _rows(n=6):
    """合成 6 行特征（列序与 features.FEATURES 一致，board 在最后）。"""
    from stock_selector.conviction import features

    return np.tile(np.array([0.01, 0.3, 0.9, 1.2, 50.0, 25.0, 0.03, 0.0, 0.0],
                            dtype="float32"), (n, 1))


# ---------------------------------------------------------------------------
# 网格 + 标签
# ---------------------------------------------------------------------------
def test_grid_contains_the_operating_range_and_the_current_event():
    assert mod.BARRIER_GRID == sorted(mod.BARRIER_GRID)
    assert min(mod.BARRIER_GRID) <= 0.02 and max(mod.BARRIER_GRID) >= 0.15
    assert 0.10 in mod.BARRIER_GRID          # x=10% 那一层必须与现 −10% 臂同事件（R3 靠它对照）


def test_label_is_the_event_at_each_line():
    """close=10、未来最低价 8.95：线在 9.8/9.7/9.5/9.25/9.0 时都算碰到，8.5 时没碰到。"""
    y = mod.barrier_labels(np.array([10.0]), np.array([8.95]), mod.BARRIER_GRID)
    want = [1 if 8.95 <= 10.0 * (1 - x) else 0 for x in mod.BARRIER_GRID]
    assert list(y[0]) == want
    assert y[0][mod.BARRIER_GRID.index(0.10)] == 1        # 9.0 那条线碰到了
    assert y[0][mod.BARRIER_GRID.index(0.15)] == 0        # 8.5 还没到


def test_expand_pairs_every_barrier_with_the_same_group_id():
    x_base, group = _rows(3), None
    close = np.array([10.0, 10.0, 10.0], dtype="float32")
    fwd_low = np.array([9.4, 8.0, 8.95], dtype="float32")
    X, y, g = mod.expand_barriers(x_base, close, fwd_low, mod.BARRIER_GRID)
    k = len(mod.BARRIER_GRID)
    assert X.shape == (3 * k, x_base.shape[1] + 1)
    assert len(y) == len(g) == 3 * k
    assert X[:k, -1].tolist() == pytest.approx(list(mod.BARRIER_GRID))   # barrier 列在最后（与线上装配同序）
    assert list(g[:k]) == [0] * k and list(g[k:2 * k]) == [1] * k
    assert y.reshape(3, k)[2].tolist() == \
        [1 if 8.95 <= 10 * (1 - x) else 0 for x in mod.BARRIER_GRID]


# ---------------------------------------------------------------------------
# 折划分：整组同侧 + embargo + 去重叠（与现臂一字不差）
# ---------------------------------------------------------------------------
def test_fold_split_keeps_groups_whole_and_respects_embargo():
    n = 60
    base = _rows(n)
    close = np.full(n, 10.0, dtype="float32")
    fwd_low = np.full(n, 9.9, dtype="float32")
    day = np.arange(n).astype("int32") + 700_000          # 每天一根，60 天
    seq = np.arange(n, dtype="int32")
    X, y, g = mod.expand_barriers(base, close, fwd_low, mod.BARRIER_GRID)
    day_rep = np.repeat(day, len(mod.BARRIER_GRID))
    seq_rep = np.repeat(seq, len(mod.BARRIER_GRID))
    start, end = 700_030, 700_040
    train_idx, valid_idx = mod.fold_indices(day_rep, g, seq_rep, start, end,
                                           horizon=10, embargo=5)
    assert len(train_idx) and len(valid_idx)
    assert set(np.unique(g[train_idx])) & set(np.unique(g[valid_idx])) == set()
    assert day_rep[valid_idx].min() >= start and day_rep[valid_idx].max() < end
    assert day_rep[train_idx].max() < start - 5
    assert set(seq_rep[valid_idx] % 10) == {0}            # 验证窗口去重叠


# ---------------------------------------------------------------------------
# 单调约束：只加在 barrier 列
# ---------------------------------------------------------------------------
def test_monotone_constraint_only_on_the_barrier_column():
    from stock_selector.conviction import features

    class A:
        lr, num_leaves, threads, seed = 0.05, 31, 4, 7

    params = mod.lgb_params_with_barrier(A())
    mc = params["monotone_constraints"]
    assert len(mc) == len(features.FEATURES) + 1
    assert mc[-1] == -1                                   # x 越大 ⇒ 概率只能不增
    assert all(v == 0 for v in mc[:-1])                   # 其余特征不许被方向约束


def test_monotonicity_checker_counts_violations():
    """R1 的仪器本身要有测试：predict 随 x 递减=合规，递增=每档都违例。"""
    grid = [0.02, 0.05, 0.10, 0.15]
    base = _rows(1)[0]
    ok = mod.monotonicity_violations(lambda v: -10.0 * float(v[-1]), [base], grid)
    assert ok == 0
    bad = mod.monotonicity_violations(lambda v: 10.0 * float(v[-1]), [base], grid)
    assert bad == len(grid) - 1


# ---------------------------------------------------------------------------
# 分层校准：isotonic 曲线（连续、单调、没区分度时诚实塌平）
# ---------------------------------------------------------------------------
def test_isotonic_curve_is_non_decreasing_and_keeps_counts():
    rng = np.random.default_rng(11)
    score = rng.uniform(0.05, 0.6, 30_000)
    label = (rng.random(score.size) < 0.05 + 0.5 * score).astype(np.int8)
    curve = mod.fit_isotonic(score, label)
    assert set(curve) >= {"xs", "ys", "n"}
    assert curve["n"] == score.size
    ys = np.asarray(curve["ys"])
    assert all(a <= b + 1e-12 for a, b in zip(ys, ys[1:]))
    assert max(ys) - min(ys) > 0.1                        # 真有信号时不许抹平


def test_isotonic_on_noise_collapses_instead_of_inventing_a_slope():
    rng = np.random.default_rng(5)
    score = rng.uniform(0, 1, 20_000)
    label = (rng.random(score.size) < 0.2).astype(np.int8)   # 与分数无关
    curve = mod.fit_isotonic(score, label)
    ys = np.asarray(curve["ys"])
    # 门槛 = 分箱本身的抽样噪声量级：16 箱 × 1250 行 ⇒ 每箱率 sd=√(0.2·0.8/1250)≈1.1pp，
    # 16 个箱极差的期望约 4·sd≈4.5pp。低于它就是「斜率和噪声分不出来」，不许印成趋势。
    assert max(ys) - min(ys) < 0.05
    assert abs(float(np.mean(ys)) - 0.2) < 0.02               # 水平还得落在实测基准上


def test_subsample_is_deterministic_and_position_free():
    codes = np.array([f"{i:06d}" for i in range(200)])
    dates = pd.to_datetime(pd.Series(["2024-01-02"] * 100 + ["2024-03-04"] * 100))
    m1 = mod.sample_groups(codes, dates, frac=0.5)
    m2 = mod.sample_groups(codes[::-1], dates[::-1], frac=0.5)
    assert list(m1) == list(m2[::-1])                    # 只依赖 (code, date) 内容
    assert 0.4 < m1.mean() < 0.6
