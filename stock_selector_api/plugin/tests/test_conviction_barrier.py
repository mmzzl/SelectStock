"""成本锚可信度（§50）：概率问的是「会不会跌破我成本的 −10%」，不是「从现在再跌 10%」。

用户原话：「10 元成本买入，我要的是这个下跌 10%，不是当前价格再下跌 10% 的概率」。
线 = 成本×0.9 是一个**钉死的绝对价**；现价往它靠 ⇒ 概率只能升。模型侧的 barrier 列
（=还要跌多少才碰到线，相对现价）由 `_barrier_gap` 算，训练/服务同一套装配（features 模块）。

这里不重训：注入假 Booster + 合成 meta，锁住「插件读哪个臂、喂进去的 barrier 是多少、
什么时候不给分、文案印的是哪一口径」。真臂的判据（单调性、同尺 AUC、分层校准）在
`tests/test_barrier_model.py` 与验收脚本里。
"""
import json

import numpy as np
import pytest

from portfolio import conviction
from stock_selector.conviction import features

from test_conviction import DOWN_SIDE, META, ROW, FakeBooster   # 复用同一套合成 meta

# barrier 网格：训练与 meta 都写死，服务时落网格外按最近两档内插
BARRIER = {
    "model_file": "barrier_model.lgb.txt",
    "num_iteration": 15,
    "horizon": 10,
    "drop": 0.10,
    "barriers": [0.02, 0.05, 0.10, 0.15],
    "features": [*META["features"], "barrier"],
    "label_contract": {"horizon": 10, "anchor": "cost",
                       "meaning": "P(未来10个交易日内最低价 <= close*(1-x))，x=还要跌多少才碰到成本线"},
    # 每个 barrier 层一条 isotonic 曲线；这里给恒等曲线，让断言只反映「喂进去的 x」
    "calibration": [{"x": b, "xs": [0.0, 1.0], "ys": [0.0, 1.0], "n": 90_000}
                    for b in (0.02, 0.05, 0.10, 0.15)],
    "base_rate_by_barrier_pct": {"0.10": 18.2},
    # 每档自己的留出 AUC：混 7 档的 pooled 天生虚高（x=2% 层基准 74.8% vs x=15% 层 6.7%，
    # 把「档位本身的基准差」当成模型本事），所以文案要印这一档的数，不能印混算的那个数。
    "layer_stats": {"0.02": {"calib_n": 90_000, "holdout_auc": 0.66, "base_rate_pct": 74.8},
                    "0.05": {"calib_n": 90_000, "holdout_auc": 0.69, "base_rate_pct": 44.8},
                    "0.1": {"calib_n": 90_000, "holdout_auc": 0.70, "base_rate_pct": 17.1},
                    "0.15": {"calib_n": 90_000, "holdout_auc": 0.74, "base_rate_pct": 6.7}},
    "validation": {"pooled": {"auc": 0.83}},
}
META_BAR = {**META, "downside": DOWN_SIDE, "barrier": BARRIER}

_ORIG_PACKAGE = conviction._package


@pytest.fixture(autouse=True)
def _restore_package():
    """注入走的是模块函数替换，用完必须还原（否则用例之间互相污染 `_package`）。"""
    yield
    conviction._package = _ORIG_PACKAGE
    conviction._cache, conviction._cache_key, conviction._arm_paths = None, (), ()


class EchoBooster:
    """分数 = 0.5 − 2×barrier（x 越小 ⇒ 分越高）：单调性由真模型保证，这里只验插件方向。"""

    def __init__(self):
        self.calls = []

    def predict(self, data, num_iteration=None):
        vec = data[0]
        self.calls.append((list(vec), num_iteration))
        return [0.5 - 2.0 * float(vec[-1])]

    def num_feature(self):
        return len(BARRIER["features"])


def _inject_barrier(cost_close_boosters=True):
    bar = EchoBooster()
    conviction._package = lambda: {
        "meta": META_BAR, "booster": FakeBooster(0.30),
        "downside_booster": FakeBooster(0.25), "barrier_booster": bar,
    }
    return bar


# ---------------------------------------------------------------------------
# 50.2 成本线换算
# ---------------------------------------------------------------------------
def test_cost_line_is_absolute_and_never_moves():
    assert conviction._cost_line(10.0, 0.10) == pytest.approx(9.0)
    assert conviction._cost_line(13.66, 0.10) == pytest.approx(12.294)
    assert conviction._cost_line(0.0, 0.10) is None
    assert conviction._cost_line(None, 0.10) is None


def test_barrier_gap_is_remaining_drop_to_the_line():
    """现价离线还差多少（相对现价）：价跌 ⇒ gap 缩小 ⇒ 概率只能升。"""
    line = conviction._cost_line(10.0, 0.10)
    assert conviction._barrier_gap(10.0, line) == pytest.approx(0.10)
    assert conviction._barrier_gap(9.2, line) == pytest.approx(1 - 9.0 / 9.2)
    assert conviction._barrier_gap(9.9, line) > conviction._barrier_gap(9.2, line)
    assert conviction._barrier_gap(8.9, line) < 0          # 已经在线下面
    assert conviction._barrier_gap(0.0, line) is None


def test_feature_vector_appends_barrier_last():
    """列序必须和训练一致（barrier 在 board 之后）——错位不报错，只会给出错的概率。"""
    assert features.FEATURES_WITH_BARRIER == [*features.FEATURES, "barrier"]
    vec, ok = features.row_to_vector_with_barrier({**ROW, "close": 9.2}, 0.0217)
    assert ok and len(vec) == len(features.FEATURES) + 1
    assert vec[-1] == pytest.approx(0.0217)


# ---------------------------------------------------------------------------
# 50.2 核心行为：现价越靠近成本线，印出来的概率必须越高
# ---------------------------------------------------------------------------
def test_probability_rises_as_price_approaches_cost_line():
    _inject_barrier()
    far = conviction.evaluate({**ROW, "close": 9.9}, action="sell", cost_price=10.0)
    near = conviction.evaluate({**ROW, "close": 9.2}, action="sell", cost_price=10.0)
    assert near["conviction_pct"] > far["conviction_pct"]      # 9.2 更危险
    assert far["conviction_pct"] == pytest.approx(round((0.5 - 2 * (1 - 9 / 9.9)) * 100, 1), abs=0.1)


def test_hold_is_complement_and_event_value_is_printed():
    _inject_barrier()
    sell = conviction.evaluate({**ROW, "close": 9.2}, action="sell", cost_price=10.0)
    hold = conviction.evaluate({**ROW, "close": 9.2}, action="hold", cost_price=10.0)
    assert hold["conviction_pct"] == pytest.approx(100.0 - sell["conviction_pct"], abs=0.1)
    assert "持有可信度" in hold["conviction_text"]
    assert str(sell["conviction_pct"]) in hold["conviction_text"]   # 原值一定露出来


def test_barrier_value_is_fed_to_the_model_as_last_column():
    bar = _inject_barrier()
    conviction.evaluate({**ROW, "close": 9.2}, action="sell", cost_price=10.0)
    vec, num_iteration = bar.calls[-1]
    assert num_iteration == BARRIER["num_iteration"]
    assert len(vec) == len(BARRIER["features"])
    assert vec[-1] == pytest.approx(1 - 9.0 / 9.2, abs=1e-6)


def test_text_names_the_absolute_line_and_remaining_gap():
    _inject_barrier()
    out = conviction.evaluate({**ROW, "close": 9.2}, action="sell", cost_price=10.0)["conviction_text"]
    assert "成本" in out and "9.00" in out                  # 线是绝对价，不是百分比口号
    assert "2.2%" in out                                   # 还差多少碰到线
    assert "N=90,000" in out                               # 样本外独立窗口数照旧要露


def test_text_prints_this_layers_auc_not_the_pooled_mix():
    """近档（0.02）自己的留出 AUC 才配和这个读数并列；混 7 档的 pooled 不可比。"""
    _inject_barrier()
    out = conviction.evaluate({**ROW, "close": 9.2}, action="sell", cost_price=10.0)["conviction_text"]
    assert "AUC0.66" in out                                 # layer_stats["0.02"].holdout_auc
    assert "0.83" not in out                                # pooled（混档，虚高）不许露出


def test_text_falls_back_to_pooled_with_its_caveat():
    """老 meta 没有 layer_stats：可以印 pooled，但必须标明是全档混算，不能装作是这一档的。"""
    bar = {k: v for k, v in BARRIER.items() if k != "layer_stats"}
    meta = {**META_BAR, "barrier": bar}
    conviction._package = lambda: {
        "meta": meta, "booster": FakeBooster(0.30),
        "downside_booster": FakeBooster(0.25), "barrier_booster": EchoBooster()}
    out = conviction.evaluate({**ROW, "close": 9.2}, action="sell", cost_price=10.0)["conviction_text"]
    assert "AUC0.83" in out and "混算" in out


def test_beyond_grid_uses_boundary_layer_and_says_upper_bound():
    """成本 10、现价 20 ⇒ 还差 55% 才碰线，超出网格（最大 15%）：给边界值并说明是上限。"""
    _inject_barrier()
    out = conviction.evaluate({**ROW, "close": 20.0}, action="sell", cost_price=10.0)
    assert out["conviction_pct"] is not None
    assert "上限" in out["conviction_text"]


# ---------------------------------------------------------------------------
# 50.2 已破线不编数（R6）
# ---------------------------------------------------------------------------
def test_below_cost_line_gives_no_probability():
    _inject_barrier()
    out = conviction.evaluate({**ROW, "close": 8.9}, action="sell", cost_price=10.0)
    assert out["conviction_pct"] is None and out["conviction_level"] is None
    assert "已跌破" in out["conviction_text"]
    assert "概率" not in out["conviction_text"]              # 不印 100%，也不印外推值
    assert out["take_profit_price"] == 9.79                 # 展示块照常（止盈价与口径无关）


# ---------------------------------------------------------------------------
# 50.3 校准：连续内插，不再有档内平台（26 天同值的成因）
# ---------------------------------------------------------------------------
CURVE = {"xs": [0.1, 0.5, 0.9], "ys": [0.02, 0.2, 0.5]}


def test_calibration_interpolates_instead_of_stuck_on_a_step():
    assert conviction._cal_value(CURVE, 0.3) == pytest.approx(0.11)
    assert conviction._cal_value(CURVE, 0.31) != conviction._cal_value(CURVE, 0.3)
    assert conviction._cal_value(CURVE, 0.05) == pytest.approx(0.02)   # 下界外钳首值
    assert conviction._cal_value(CURVE, 0.95) == pytest.approx(0.5)    # 上界外钳末值


def test_per_barrier_layer_curves_are_used_and_interpolated_in_x():
    """x 落在网格外 ⇒ 取最近两档各自曲线的值，再按 x 线性内插（7 条曲线，不是一条混的）。"""
    meta = {**META_BAR, "barrier": {**BARRIER, "calibration": [
        {"x": 0.02, "xs": [0.0, 1.0], "ys": [0.0, 0.2], "n": 90_000},    # 恒等×0.2
        {"x": 0.05, "xs": [0.0, 1.0], "ys": [0.0, 0.4], "n": 90_000},    # 恒等×0.4
    ]}}
    bar = EchoBooster()
    conviction._package = lambda: {"meta": meta, "booster": FakeBooster(0.30),
                                  "downside_booster": FakeBooster(0.25),
                                  "barrier_booster": bar}
    # 现价 9.35 ⇒ x = 1 − 9/9.35 = 0.037435，两档之间的内插点
    # 每层各自把 score=0.5 映射成 0.2×0.5=0.10 与 0.4×0.5=0.20，再按 x 线性内插（§50.3）
    lo, hi = 0.2 * 0.5, 0.4 * 0.5
    w = (0.037435 - 0.02) / (0.05 - 0.02)
    p = conviction._barrier_probability(meta["barrier"], 0.037435, score=0.5)
    assert p == pytest.approx(lo + w * (hi - lo), abs=1e-3)


# ---------------------------------------------------------------------------
# 50.5 退化与回退：缺成本 / 缺臂 / 缺动作 ⇒ 走旧口径，且文案写清是哪一口径
# ---------------------------------------------------------------------------
def test_without_cost_price_falls_back_to_daily_close_anchor():
    _inject_barrier()
    out = conviction.evaluate({**ROW, "close": 9.2}, action="sell")
    assert "当日收盘" in out["conviction_text"]              # 旧口径必须自报锚，别和新口径混读
    assert "成本" not in out["conviction_text"]


def test_old_downside_text_names_its_anchor():
    """现 −10% 臂的文案也要改口：锚是当日收盘，不是持仓成本（否则两条口径看起来一模一样）。"""
    conviction._package = lambda: {"meta": META_DOWN_ONLY, "booster": FakeBooster(0.30),
                                  "downside_booster": FakeBooster(0.25)}
    out = conviction.evaluate({**ROW, "close": 9.2}, action="sell")
    assert "当日收盘" in out["conviction_text"]


META_DOWN_ONLY = {**META, "downside": DOWN_SIDE}


def test_barrier_arm_absent_but_cost_given_still_falls_back():
    conviction._package = lambda: {"meta": META_DOWN_ONLY, "booster": FakeBooster(0.30),
                                   "downside_booster": FakeBooster(0.25)}
    out = conviction.evaluate({**ROW, "close": 9.2}, action="hold", cost_price=10.0)
    assert "当日收盘" in out["conviction_text"]


def test_still_no_score_for_st_and_missing_features():
    _inject_barrier()
    assert conviction.evaluate({**ROW, "name": "*ST海润", "close": 9.2},
                               action="sell", cost_price=10.0)["conviction_pct"] is None
    _inject_barrier()
    out = conviction.evaluate({k: v for k, v in ROW.items()
                               if k not in ("pos60", "ma20")}, action="sell", cost_price=10.0)
    assert out["conviction_pct"] is None


# ---------------------------------------------------------------------------
# 50.5 接线：analyzer 必须把成本传进来（cost 早就在它作用域里）
# ---------------------------------------------------------------------------
def test_analyzer_passes_cost_price(monkeypatch):
    from portfolio import analyzer

    seen = {}

    def fake_evaluate(row, action=None, cost_price=None):
        seen["action"] = action
        seen["cost_price"] = cost_price
        return {"conviction_pct": None, "conviction_level": None, "conviction_text": None,
                "take_profit_price": None, "game_v": None, "game_s": None, "game_text": None}

    monkeypatch.setattr(analyzer.conviction, "evaluate", fake_evaluate)

    class P:
        @staticmethod
        def latest_row(code):
            return {**ROW, "code": code}

        @staticmethod
        def chip_levels(code, window=60):
            return None

    analyzer.analyze_holdings([{"code": "600000", "name": "甲", "quantity": 100,
                                "cost_price": 13.66}], P())
    assert seen["cost_price"] == pytest.approx(13.66)
    assert seen["action"] in ("hold", "sell")


# ---------------------------------------------------------------------------
# 50.5 回滚：一条 mv 就把成本锚档摘掉，不能要求重启服务
# ---------------------------------------------------------------------------
def _tiny_booster(tmp_path, n_features: int, name: str):
    """真 LightGBM 模型（列数对得上 meta），因为要测的就是 `_load` 那条按列数拒绝的路径。"""
    import lightgbm as lgb

    rng = np.random.default_rng(1)
    X = rng.uniform(0, 1, size=(400, n_features))
    y = (rng.uniform(0, 1, 400) > 0.5).astype(int)
    booster = lgb.train({"verbose": -1, "min_data_in_bin": 1, "feature_fraction": 1.0},
                        lgb.Dataset(X, label=y, categorical_feature=[n_features - 1]),
                        num_boost_round=3)
    path = tmp_path / name
    booster.save_model(str(path))
    return path


def _write_real_package(tmp_path, monkeypatch):
    main = _tiny_booster(tmp_path, len(META["features"]), "c.lgb.txt")
    bar = _tiny_booster(tmp_path, len(BARRIER["features"]), "barrier_model.lgb.txt")
    meta = {**META, "model_file": main.name, "num_iteration": 3,
            "barrier": {**BARRIER, "model_file": bar.name, "num_iteration": 3}}
    meta_path = tmp_path / "conviction_model.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("CONVICTION_MODEL_FILE", str(main))
    monkeypatch.setenv("CONVICTION_META_FILE", str(meta_path))
    conviction._cache, conviction._cache_key, conviction._arm_paths = None, (), ()
    return bar


def test_moving_the_barrier_file_away_rolls_back_without_restart(tmp_path, monkeypatch):
    """§50.5 写的回滚是 `mv barrier_model.lgb.txt{,.off}`：缓存键必须盯着那两臂的文件。

    只盯主模型 + meta 时，摘掉臂文件后进程里还是旧 pack（带 barrier_booster），
    App 继续印成本锚读数——「一条命令就能回滚」就成了假话。
    """
    bar = _write_real_package(tmp_path, monkeypatch)
    assert "barrier_booster" in conviction._package()

    off = bar.with_suffix(".off")
    bar.rename(off)
    assert "barrier_booster" not in conviction._package()      # 不重启、不改 conf 就退档

    off.rename(bar)
    assert "barrier_booster" in conviction._package()          # 挪回来又生效
