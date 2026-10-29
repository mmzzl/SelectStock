"""可信度模型脚本的口径测试：校准分档保单调、越界取值、相对基准定档。

训练本身（净化前推、去重叠）要跑十几分钟且依赖全量样本，这里只锁住「写进 meta 的那张
校准表和线上取值的规则」——它们直接决定 App 上那个百分比是不是可核对的。
"""
import importlib.util
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "train_conviction_model.py"


def _load():
    spec = importlib.util.spec_from_file_location("train_conviction_model", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load()


def _check_monotone(table):
    rates = [r["rate"] for r in table]
    assert rates == sorted(rates), rates


def test_calibrate_is_monotone_and_conserves_counts():
    rng = np.random.default_rng(3)
    score = rng.uniform(0.2, 0.5, 60_000)
    # 只让分数带一点点真实区分度，并故意在高分段制造违序，PAVA 必须合并掉
    label = (rng.random(score.size) < 0.25 + 0.3 * (score - 0.2)).astype(np.int8)
    table = mod.calibrate(score, label, bins=10)
    _check_monotone(table)
    assert sum(r["n"] for r in table) == score.size
    assert table[-1]["rate"] > table[0]["rate"] + 0.05     # 真有区分度时不能被抹平


def test_calibrate_all_identical_scores_gives_no_table():
    """所有样本外分数挤在一个值上 = 模型没有区分度：返回空表，让调用方退化而不是编一档。"""
    score = np.full(1000, 0.3)
    label = np.random.default_rng(0).integers(0, 2, 1000).astype(np.int8)
    assert mod.calibrate(score, label, bins=10) == []


def test_lookup_clamps_outside_edges():
    table = [{"lo": 0.2, "hi": 0.3, "rate": 0.3, "n": 10},
             {"lo": 0.3, "hi": 0.4, "rate": 0.35, "n": 10}]
    assert mod.calibrate_lookup(table, 0.25)["rate"] == 0.3
    assert mod.calibrate_lookup(table, 0.05)["rate"] == 0.3      # 下界外 → 首档
    assert mod.calibrate_lookup(table, 0.95)["rate"] == 0.35     # 上界外 → 末档
    assert mod.calibrate_lookup([], 0.3) is None


def test_level_is_relative_not_absolute():
    """剧本成功率天然 ~30%：绝对阈值（>=60）会把所有票判成「低」，等级就没信息量了。"""
    assert mod.level_of(33.0, 31.0) == "高"
    assert mod.level_of(31.0, 31.0) == "中"
    assert mod.level_of(29.0, 31.0) == "低"
    assert mod.level_of(20.0, 0.0) == "低"          # 拿不到基准就不标档


def test_feature_contract_matches_serving_module():
    """训练脚本与线上插件共用 features.py：这里锁住列序不被单边改掉。"""
    from stock_selector.conviction import features as fx

    assert mod.FEATURES == fx.FEATURES
    assert mod.NUMERIC_FEATURES == fx.NUMERIC_FEATURES
    assert mod.BOARD_COL == fx.BOARD_COL == len(fx.NUMERIC_FEATURES)
    assert mod.REQUIRED_FEATURES == fx.REQUIRED_FEATURES


# ---------------------------------------------------------------------------
# 下臂（P(未来10日最低价跌破 close*(1-drop))）的建样本口径
# ---------------------------------------------------------------------------
def _tiny_samples(tmp_path):
    """6 行合成样本：2 行中性（label=-1）、fwd_low 覆盖 9.0 阈值两侧。"""
    import pandas as pd

    df = pd.DataFrame({
        "date": pd.date_range("2020-01-01", periods=6),
        "code": ["600000"] * 6,
        "close": [10.0] * 6,
        "board": ["主板"] * 6,
        "bias20": [0.01] * 6, "pos60": [0.5] * 6, "vol_ratio": [1.0] * 6,
        "vol_z": [0.2] * 6, "rsi14": [50.0] * 6, "adx14": [20.0] * 6,
        "atr_n": [0.02] * 6, "lu": [0] * 6,
        "label": [1, 0, -1, -1, 0, 1],
        "fwd_low": [9.5, 8.9, 9.9, 8.0, 9.0, 9.4],
    })
    path = tmp_path / "s.parquet"
    df.to_parquet(path, index=False)
    return path


def test_downside_arm_keeps_neutral_and_swaps_label(tmp_path):
    """下臂吃全样本（不剔中性）：label 换成 drop10 事件；9.0 恰在阈值上算触发。"""
    path = _tiny_samples(tmp_path)
    df = mod.load_samples(path, set(), 10, 0, resolved_only=False)
    assert len(df) == 6
    assert df["label"].tolist() == [0, 1, 0, 1, 1, 0]


def test_upside_arm_unchanged_by_new_flag(tmp_path):
    path = _tiny_samples(tmp_path)
    df = mod.load_samples(path, set(), 10)                 # 默认 resolved_only=True
    assert len(df) == 4
    assert df["label"].tolist() == [1, 0, 0, 1]            # 中性行（label=-1）被剔


# ---------------------------------------------------------------------------
# 每晚重训写 meta 时不能把「这次没训的臂」抹掉（§50 成本锚臂由 train_barrier_model.py 训）
# ---------------------------------------------------------------------------
def test_carry_over_keeps_barrier_block():
    """本脚本只重写上涨/下行两臂；barrier 块必须原样带进新 meta。"""
    fresh = {"version": 2, "features": ["a"], "downside": {"version": 1}}
    prev = {"barrier": {"model_file": "barrier_model.lgb.txt", "drop": 0.10}}
    carried = mod.carry_over_arms(fresh, prev)
    assert carried == ("barrier",)
    assert fresh["barrier"] == prev["barrier"]


def test_carry_over_never_clobbers_this_runs_block():
    """同名块以本次为准（万一以后本脚本也训这臂，旧块不能盖掉新读数）。"""
    fresh = {"barrier": {"model_file": "new.lgb.txt"}}
    prev = {"barrier": {"model_file": "old.lgb.txt"}}
    assert mod.carry_over_arms(fresh, prev) == ()
    assert fresh["barrier"]["model_file"] == "new.lgb.txt"


def test_carry_over_tolerates_missing_previous_meta():
    """首次运行/旧文件损坏：不能因为拿不到旧 meta 就不写新 meta。"""
    fresh = {"downside": {}}
    assert mod.carry_over_arms(fresh, {}) == ()
    assert mod.carry_over_arms(fresh, None) == ()
