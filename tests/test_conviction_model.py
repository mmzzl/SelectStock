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
