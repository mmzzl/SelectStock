"""可信度插件读取测试：模型打分、校准档取值、档位、文案、缺数退化。

模型由 scripts/train_conviction_model.py 产出；这里不重训，用假 Booster + 合成 meta 锁
「插件怎么读、什么时候不给分、文案里印的是哪个 N」。特征装配本身归 test_conviction_features.py。
"""
import json

import pytest

from portfolio import conviction


class FakeBooster:
    def __init__(self, score):
        self.score = score
        self.calls = []

    def predict(self, data, num_iteration=None):
        self.calls.append((data, num_iteration))
        return [self.score]

    def num_feature(self):
        return len(META["features"])


META = {
    "features": ["bias20", "pos60", "vol_ratio", "vol_z", "rsi14", "adx14", "atr_n", "lu", "board"],
    "label_contract": {"horizon": 10, "stop": 0.05, "take": 0.10, "neutral_excluded": True},
    "base_rate_pct": 30.9,
    "num_iteration": 12,
    "validation": {"pooled": {"auc": 0.5567, "n": 400000, "base_rate": 0.309}},
    "discrimination": {"spread_pp": 7.1},
    "calibration": [
        {"lo": 0.00, "hi": 0.25, "rate": 0.2450, "n": 40120},
        {"lo": 0.25, "hi": 0.28, "rate": 0.2890, "n": 120000},
        {"lo": 0.28, "hi": 0.33, "rate": 0.3150, "n": 180000},
        {"lo": 0.33, "hi": 0.40, "rate": 0.3500, "n": 60000},
    ],
}

# 600000 主板；bias20 = 10.0/10.1-1 ≈ -0.0099；pos60 0.3；vol_ratio 0.9
ROW = {"code": "600000", "name": "浦发银行", "close": 10.0, "ma20": 10.1, "pos60": 0.3,
       "vol_ratio": 0.9, "vol_z": 1.2, "rsi14": 50.0, "adx14": 25.0, "atr14": 0.35, "lu": 0.0,
       "res20": 11.0, "sup20": 9.5}


_ORIG_PACKAGE = conviction._package


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    """默认没有模型文件：每个用例自己决定要不要注入。"""
    monkeypatch.setenv("CONVICTION_MODEL_FILE", str(tmp_path / "missing.lgb.txt"))
    monkeypatch.setenv("CONVICTION_META_FILE", str(tmp_path / "missing.json"))
    conviction._cache, conviction._cache_key, conviction._arm_paths = None, (), ()
    conviction._package = _ORIG_PACKAGE
    yield
    conviction._cache, conviction._cache_key, conviction._arm_paths = None, (), ()
    conviction._package = _ORIG_PACKAGE


def _inject(score, meta=None):
    """塞一个假 Booster：文件缺失时 `_package` 会直接退化，所以注入走这一层。"""
    booster = FakeBooster(score)
    conviction._package = lambda: {"meta": meta or META, "booster": booster}
    return booster


def test_evaluate_uses_calibrated_segment_rate():
    _inject(0.30)                                       # 落在 0.28-0.33 档 → 31.5%
    out = conviction.evaluate(ROW)
    assert out["conviction_pct"] == 31.5
    assert out["conviction_level"] == "中"
    assert out["take_profit_price"] == 11.0             # 10.0 * (1+0.10)
    assert "独立窗口N=180,000" in out["conviction_text"]
    assert "基准30.9%" in out["conviction_text"] and "AUC0.56" in out["conviction_text"]
    assert "95%CI" not in out["conviction_text"]        # 不再有虚高样本量推出来的假置信区间


def test_level_is_relative_to_base_rate():
    _inject(0.36)                                       # 档 35.0% ≥ 30.9*1.05 → 高
    assert conviction.evaluate(ROW)["conviction_level"] == "高"
    _inject(0.20)                                       # 档 24.5% ≤ 30.9*0.95 → 低
    assert conviction.evaluate(ROW)["conviction_level"] == "低"
    assert conviction._level(32.5, 30.9) == "高" and conviction._level(29.3, 30.9) == "低"
    assert conviction._level(31.0, 30.9) == "中"
    assert conviction._level(50.0, 0.0) == "低"         # 拿不到基准就不标档


def test_score_outside_table_clamps_to_nearest_segment():
    _inject(0.9)
    assert conviction.evaluate(ROW)["conviction_pct"] == 35.0
    _inject(0.01)
    assert conviction.evaluate(ROW)["conviction_pct"] == 24.5


def test_level_uses_its_own_board_benchmark():
    """基金摸 +10% 本来就比股票难：拿全市场均值当基准会让所有基金永远「低」。"""
    meta = {**META, "base_rate_by_board_pct": {"主板": 30.9, "场内基金": 22.6}}
    _inject(0.10, meta)                                 # 档 24.5%：主板低、基金高
    assert conviction.evaluate(ROW)["conviction_level"] == "低"
    fund = conviction.evaluate({**ROW, "code": "510300", "name": "沪深300ETF"})
    assert fund["conviction_level"] == "高"
    assert "同类基准22.6%" in fund["conviction_text"]


def test_st_holding_gets_no_number_but_keeps_take_profit_and_game():
    """ST 的 ±5% 涨跌停会把 +10%/-5% 屏障几何改掉，训练集里没有它，就不给它的分。"""
    _inject(0.30)
    out = conviction.evaluate({**ROW, "name": "*ST海润"})
    assert out["conviction_pct"] is None and out["conviction_level"] is None
    assert out["conviction_text"] is None
    assert out["take_profit_price"] == 11.0 and out["game_v"] is not None


def test_missing_core_features_degrade():
    _inject(0.30)
    for key in ("pos60", "vol_ratio"):
        out = conviction.evaluate({k: v for k, v in ROW.items() if k != key})
        assert out["conviction_pct"] is None and out["take_profit_price"] == 11.0
    # ma20 缺但 close 在 → bias20 不可判，同样不给分
    out = conviction.evaluate({k: v for k, v in ROW.items() if k != "ma20"})
    assert out["conviction_pct"] is None


def test_no_model_degrades_to_none_but_game_still_available():
    out = conviction.evaluate(ROW)
    assert out["conviction_pct"] is None and out["conviction_level"] is None
    assert out["conviction_text"] is None and out["take_profit_price"] is None
    assert out["game_v"] is not None and out["game_text"]     # 博弈代理与模型无关
    assert conviction.evaluate(None)["conviction_pct"] is None


def test_model_meta_column_mismatch_is_rejected(tmp_path):
    """模型列数与 meta 不符（拿错版本/漏训一列）必须整体退化，不能按错位打分。"""
    model = _tiny_model(tmp_path)
    meta = {**META, "features": META["features"][:-1]}
    path = tmp_path / "short.json"
    path.write_text(json.dumps(meta), encoding="utf-8")
    assert conviction._load(model, path) == {}


def test_real_booster_end_to_end(tmp_path, monkeypatch):
    """用真 LightGBM Booster 走一遍打分：假 Booster 挡不住「喂错形状/列序」这类错。"""
    model = _tiny_model(tmp_path)
    meta_path = tmp_path / "meta.json"
    meta_path.write_text(json.dumps({**META, "num_iteration": 2}), encoding="utf-8")
    monkeypatch.setenv("CONVICTION_MODEL_FILE", str(model))
    monkeypatch.setenv("CONVICTION_META_FILE", str(meta_path))
    out = conviction.evaluate(ROW)
    assert out["conviction_pct"] in {round(s["rate"] * 100, 1) for s in META["calibration"]}
    assert out["conviction_level"] in {"高", "中", "低"}
    assert out["take_profit_price"] == 11.0


def _tiny_model(tmp_path):
    """三棵树的随机小模型：只为产出可加载的 .lgb.txt，不代表任何业务口径。"""
    lgb = pytest.importorskip("lightgbm")
    import numpy as np

    rng = np.random.default_rng(0)
    n, k = 400, len(META["features"])
    x = rng.normal(size=(n, k)).astype("float32")
    x[:, -1] = rng.integers(0, len(META["calibration"]), n)     # board 走类别下标
    y = (x[:, 0] + rng.normal(size=n) * 0.5 > 0).astype(np.int8)
    booster = lgb.train({"objective": "binary", "verbosity": -1, "num_leaves": 5,
                         "min_data_in_leaf": 5, "learning_rate": 0.3, "seed": 1},
                        lgb.Dataset(x, label=y, categorical_feature=[k - 1]),
                        num_boost_round=3)
    path = tmp_path / "tiny.lgb.txt"
    booster.save_model(str(path))
    return path


def test_weak_discrimination_is_marked_in_text():
    _inject(0.30, {**META, "discrimination": {"spread_pp": 1.2}})
    assert conviction.evaluate(ROW)["conviction_text"].endswith("·区分度弱")


def test_num_iteration_passed_to_predict():
    booster = _inject(0.30)
    conviction.evaluate(ROW)
    data, num_iteration = booster.calls[-1]
    assert num_iteration == META["num_iteration"]
    assert len(data) == 1 and len(data[0]) == len(META["features"])


# ---------------------------------------------------------------------------
# 下行风险档（P(未来10日跌破−10%)）+ 动作感知可信度
# 2026-10-20 与用户定稿：conviction_pct = 对「当前显示动作」的实测支持度——
#   sell → P(跌破)，hold → 1−P(跌破)。下行模型缺失时回退原上涨口径。
# ---------------------------------------------------------------------------
DOWN_SIDE = {
    "num_iteration": 9,
    "base_rate_pct": 20.0,
    "max_probability_pct": 85.2,          # 复验窗历史最险一天的预测上限
    "validation": {"pooled": {"auc": 0.7269}},
    "label_contract": {"horizon": 10, "drop": 0.10,
                       "meaning": "P(未来10日最低价 <= close*(1-drop))，全样本、不剔中性"},
    "calibration": [
        {"lo": 0.00, "hi": 0.10, "rate": 0.019, "n": 340486},
        {"lo": 0.10, "hi": 0.20, "rate": 0.105, "n": 340485},
        {"lo": 0.20, "hi": 0.35, "rate": 0.228, "n": 680970},
        {"lo": 0.35, "hi": 0.60, "rate": 0.420, "n": 680971},
        {"lo": 0.60, "hi": 1.00, "rate": 0.600, "n": 340485},
    ],
}
META_DOWN = {**META, "downside": DOWN_SIDE}


def _inject_down(down_score, meta=None, up_score=0.30):
    """注入两个假 Booster：上涨臂固定 0.30，下行臂由用例给分。"""
    up = FakeBooster(up_score)
    down = FakeBooster(down_score)
    conviction._package = lambda: {"meta": meta or META_DOWN,
                                   "booster": up, "downside_booster": down}
    return down


def test_hold_conviction_is_complement_of_downside():
    _inject_down(0.25)                    # 档 rate 0.228 → P(跌破)=22.8%
    out = conviction.evaluate(ROW, action="hold")
    assert out["conviction_pct"] == 77.2  # 100 − 22.8
    assert out["conviction_level"] == "高"
    assert "持有可信度" in out["conviction_text"]
    assert "22.8" in out["conviction_text"]         # 事件原值必须露出来，不能只给补
    assert "不崩" in out["conviction_text"] and "会赚" in out["conviction_text"]


def test_sell_conviction_is_downside_itself():
    _inject_down(0.25)
    out = conviction.evaluate(ROW, action="sell")
    assert out["conviction_pct"] == 22.8
    assert out["conviction_level"] == "低"          # 22.8 < 50：这条卖出证据弱
    assert "卖出可信度" in out["conviction_text"]


def test_sell_conviction_level_and_ceiling_note():
    _inject_down(0.62)                    # 档 rate 0.600 → P(跌破)=60%
    out = conviction.evaluate(ROW, action="sell")
    assert out["conviction_pct"] == 60.0
    assert out["conviction_level"] == "中"          # 50<=60<75
    assert "上限" in out["conviction_text"]          # 提醒卖出侧到不了 100%
    assert "最险档样本外实测" in out["conviction_text"]  # 上限的口径要说清：是分档实测，不是某天
    assert "历史最险一天" not in out["conviction_text"]


def test_downside_falls_back_to_upside_when_model_absent():
    _inject(0.30)                         # 只有上涨臂（META 无 downside）
    assert conviction.evaluate(ROW, action="hold")["conviction_pct"] == 31.5
    assert conviction.evaluate(ROW)["conviction_pct"] == 31.5


def test_downside_st_respects_st_and_feature_degrade():
    _inject_down(0.25)
    out = conviction.evaluate({**ROW, "name": "*ST海润"}, action="hold")
    assert out["conviction_pct"] is None and out["conviction_text"] is None
    _inject_down(0.25)
    out2 = conviction.evaluate({k: v for k, v in ROW.items() if k != "pos60"},
                               action="sell")
    assert out2["conviction_pct"] is None
