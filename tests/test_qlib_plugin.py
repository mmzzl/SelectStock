"""qlib 插件的契约与口径测试：不依赖 qlib 面板、不依赖训练产物，能进 CI。

真正「分数和 scripts/qlib_run.py 一致」那条门槛是端到端比对，放在
scripts/qlib_plugin_check.py（要读 bin 面板和落盘模型，不适合当单测）。
这里守的是另外三件更容易悄悄坏掉的事：
1. 产物存/读之后列序与统计量不漂移；
2. 归一化复刻件与「÷std → clip±3 → NaN→0」的口径逐元素一致；
3. /api/selector/run 的返回形状与后端 selection_service._http_run 取的键一致
   （键错一个，页面就是静默空值，比报错难发现）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from stock_selector_qlib import artifacts, serving  # noqa: E402


def _booster():
    """真·lightgbm Booster：产物走 save_model/lgb.Booster(model_file) 的真实通道。"""
    lgb = pytest.importorskip("lightgbm")
    rng = np.random.default_rng(7)
    X = rng.normal(size=(80, 3))
    y = X @ np.array([1.0, 2.0, 3.0]) + rng.normal(scale=0.1, size=80)
    return lgb.train({"objective": "regression", "verbose": -1, "num_leaves": 4},
                     lgb.Dataset(X, label=y, feature_name=["KMID", "KLEN", "ROC60"]),
                     num_boost_round=3)


@pytest.fixture
def art_dir(tmp_path):
    dest = tmp_path / "model"
    columns = ["KMID", "KLEN", "ROC60"]
    artifacts.save(booster=_booster(), columns=columns,
                   mean=np.array([0.1, 0.2, 0.3]), std=np.array([1.0, 2.0, 4.0]),
                   pool=["SH600000", "SZ000001"],
                   meta={"run_id": "test-run", "train_end": "2021-12-31", "feature_count": 3},
                   dest=dest)
    return dest


def test_artifacts_roundtrip_keeps_column_order(art_dir):
    art = artifacts.load(art_dir)
    assert art.columns == ["KMID", "KLEN", "ROC60"]  # 列序是产物的一部分，不能靠排序碰运气
    assert art.mean.tolist() == [0.1, 0.2, 0.3]
    assert art.std.tolist() == [1.0, 2.0, 4.0]
    assert art.pool == ["SH600000", "SZ000001"]
    assert art.run_id == "test-run"
    assert art.describe()["features"] == 3


def test_save_rejects_mismatched_shapes(tmp_path):
    with pytest.raises(ValueError):
        artifacts.save(booster=_booster(), columns=["a", "b"], mean=np.zeros(3),
                       std=np.ones(2), pool=["SH600000"], meta={}, dest=tmp_path / "x")


def test_load_without_artifacts_tells_how_to_train(tmp_path):
    with pytest.raises(FileNotFoundError) as exc:
        artifacts.load(tmp_path / "nothing")
    assert "--export-model" in str(exc.value)


def _frame():
    idx = pd.MultiIndex.from_tuples(
        [(pd.Timestamp("2026-10-15"), "SH600000"), (pd.Timestamp("2026-10-16"), "SZ000001")],
        names=["datetime", "instrument"])
    return pd.DataFrame(
        [[1.1, 2.0, np.nan], [np.inf, -5.0, 0.3]], index=idx, columns=["KMID", "KLEN", "ROC60"])


def test_normalize_matches_training_caliber_elementwise(art_dir):
    """(X-mean)/std → clip(±3) → NaN 填 0，顺序不能换：clip 会把 NaN 留成 NaN，NaN 最后才填掉。"""
    art = artifacts.load(art_dir)
    out = serving.normalize(_frame(), art)
    raw = _frame().reindex(columns=art.columns).to_numpy(dtype="float64")
    expected = np.clip((raw - art.mean) / art.std, -3.0, 3.0)
    expected[np.isnan(expected)] = 0.0
    assert np.array_equal(out, expected)
    assert not np.isnan(out).any()
    assert np.isfinite(out).all(), "inf 必须在 clip 里被收进 [-3,3]，否则 LightGBM 拿到 inf"


def test_normalize_raises_on_missing_factor(art_dir):
    art = artifacts.load(art_dir)
    frame = _frame().drop(columns=["ROC60"])
    with pytest.raises(RuntimeError, match="因子缺列"):
        serving.normalize(frame, art)


def test_api_contract_matches_backend_consumption(art_dir, monkeypatch):
    """返回形状必须能被后端 _http_run 直接吃：稳定四键 + 它取的 rank/price/score/extra。"""
    from fastapi.testclient import TestClient

    from stock_selector_qlib import api

    monkeypatch.setattr(artifacts, "MODEL_DIR", art_dir)
    monkeypatch.setattr(serving, "latest_scores", lambda topk=30, refresh=False: [
        {"code": "600000", "qlib_symbol": "SH600000", "date": "2026-10-16", "score_raw": 1.234,
         "score": 0.99, "rank": 1, "price": 8.88, "universe": 300},
    ])
    monkeypatch.setattr(serving, "stock_names", lambda codes: {"600000": "浦发银行"})
    client = TestClient(api.app)

    assert client.get("/api/strategy/list").json()["data"] == ["qlib_alpha158"]
    detail = client.get("/api/strategy/detail").json()["data"][0]
    assert detail["name"] == "qlib_alpha158" and detail["as_of"] is True

    body = client.post("/api/selector/run", json={"strategies": None}).json()
    assert body["code"] == 0
    item = body["data"][0]
    for key in ("code", "name", "signal", "reason", "strategy", "asof", "price", "score",
                "rank", "extra"):
        assert key in item, f"契约键 {key} 缺失"
    assert item["code"] == "600000" and item["name"] == "浦发银行"
    assert item["asof"] == "2026-10-16" and item["rank"] == 1
    assert item["extra"]["score_raw"] == 1.234

    # 别的策略名不该出 qlib 的票
    assert client.post("/api/selector/run", json={"strategies": ["trend_rebound"]}).json()["data"] == []


def test_api_failure_is_http_error_not_empty_list(art_dir, monkeypatch):
    """打分挂了必须报 5xx：返回空名单会被后端记成「今日无命中」，把故障说成结论。"""
    from fastapi.testclient import TestClient

    from stock_selector_qlib import api

    monkeypatch.setattr(artifacts, "MODEL_DIR", art_dir)

    def boom(topk=30, refresh=False):
        raise RuntimeError("面板没更新")

    monkeypatch.setattr(serving, "latest_scores", boom)
    resp = TestClient(api.app).post("/api/selector/run", json={})
    assert resp.status_code == 502
    assert "面板没更新" in resp.text


def test_backtest_metrics_use_net_not_gross():
    """净值口径：qlib 的 return 列把手续费加回去了（毛收益），metrics 必须走 account 列。"""
    from stock_selector_qlib.backtest import _account_curve, _metrics

    report = pd.DataFrame({
        "account": [100.0, 98.0, 105.84],
        "return": [0.0, 0.02, 0.08],
        "bench": [0.0, 0.01, 0.02],
        "turnover": [0.0, 0.3, 0.3],
        "cost": [0.0, 0.002, 0.002],
    }, index=pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07"]))
    m = _metrics(report, _account_curve(report, 100.0), 100.0, [])
    assert m["total_return"] == pytest.approx(0.0584)      # 净值 +5.84%
    assert m["gross_return"] == pytest.approx(0.1016)  # 毛利 +10.16%（1.02×1.08−1）
    assert m["total_return"] < m["gross_return"]
    assert m["end_value"] == pytest.approx(105.84)
    assert m["max_drawdown"] == pytest.approx(-0.02)  # 净值从 100 跌到 98
    with pytest.raises(RuntimeError, match="account"):
        _account_curve(report.drop(columns=["account"]), 100.0)
    # 平均持有天数 = 持仓票·日 / 卖出笔数；没有卖出笔就没有分母，宁可给 None 也不给 0
    assert m["avg_hold_days"] is None
    trades = [{"side": "sell", "pnl": 1.0}, {"side": "sell", "pnl": -1.0}]
    assert _metrics(report, _account_curve(report, 100.0), 100.0, trades,
                    name_days=10)["avg_hold_days"] == pytest.approx(5.0)


def test_snap_window_tolerates_holidays_and_calendar_end():
    """页面选的日期可以是周末/节假日，末尾日撞日历最后一天要退一根（qlib 要取下一根 K 线）。"""
    from stock_selector_qlib.backtest import _snap_window

    days = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]
    assert _snap_window(days, "2026-01-03", "2026-01-06") == ("2026-01-05", "2026-01-06")
    assert _snap_window(days, "2026-01-07", "2026-01-08") == ("2026-01-07", "2026-01-07")
    with pytest.raises(RuntimeError, match="一个交易日都没有"):
        _snap_window(days, "2026-02-01", "2026-02-28")


def _band_scores():
    """当日分数榜：A 最高、E 最低。"""
    return pd.Series([5.0, 4.0, 3.0, 2.0, 1.0], index=list("ABCDE"))


def test_band_plan_holds_while_still_recommended():
    """「还在榜就拿着」：跌出出榜线才卖，空位从进榜名单按分高低补，已拿的不重复买。"""
    from stock_selector_qlib.backtest import band_plan

    scores = _band_scores()
    # 出榜线=前 4 名 ⇒ E 出局；空位从 A/B/C 里补，手里已有的 A 不再买
    assert band_plan(scores, ["A", "E"], topk=2, entry_rank=3, exit_rank=4) == (["E"], ["B"])
    # B、C 都还在榜上 ⇒ 一股不卖；满仓 ⇒ 也不买
    assert band_plan(scores, ["B", "C"], topk=2, entry_rank=3, exit_rank=4) == ([], [])
    # 空仓 ⇒ 买分数最高的两只
    assert band_plan(scores, [], topk=2, entry_rank=3, exit_rank=4) == ([], ["A", "B"])
    # 榜上没有行（停牌/断档）按「不在榜」处理，要卖；成交那关由交易所挡单负责
    assert band_plan(scores, ["F"], topk=2, entry_rank=3, exit_rank=4) == (["F"], ["A", "B"])
    # 卖出名单绝不当天又被买回：进榜是出榜的子集
    # D 排名第 4、还在出榜线内 ⇒ 继续拿；E 出局腾出 2 个空位（topk=3）⇒ 补 A、B
    sells, buys = band_plan(scores, ["D", "E"], topk=3, entry_rank=3, exit_rank=4)
    assert sells == ["E"] and buys == ["A", "B"] and not set(sells) & set(buys)


def test_band_plan_is_deterministic_on_ties():
    """同分必须按输入顺序定名次（mergesort 稳定）：随机打破平局会让回测不可复现。"""
    from stock_selector_qlib.backtest import band_plan

    scores = pd.Series([1.0, 1.0, 1.0], index=["X", "Y", "Z"])
    first = band_plan(scores, [], topk=2, entry_rank=3, exit_rank=3)
    for _ in range(3):
        assert band_plan(scores, [], topk=2, entry_rank=3, exit_rank=3) == first
    assert first == ([], ["X", "Y"])
