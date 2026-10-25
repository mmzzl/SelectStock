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

from stock_selector_qlib import artifacts, pref, serving  # noqa: E402


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

    # 这一条只钉模型榜的契约；第二条策略（hot_pref）另开一组，别让它们互相牵连
    monkeypatch.setenv("PREF_LIST", "off")
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
    # 本引擎算不出来的指标必须**逐条显式声明**（detail 里一份、每行一份）：
    # 留 null 到 App 上是「—」或整行消失，使用者会读成「没有风险」「不用设止损」。
    assert detail["na_fields"] == list(api._NA_FIELDS)
    assert item["extra"]["na_fields"] == list(api._NA_FIELDS)
    assert "risk_ex10_pp" in item["extra"]["na_fields"]

    # 别的策略名不该出 qlib 的票，也不能回 200 空名单（那会被后端当成「今天没票」）
    resp = client.post("/api/selector/run", json={"strategies": ["trend_rebound"]})
    assert resp.status_code == 400 and "策略不可用" in resp.text


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


def test_score_panel_uses_live_pool_and_can_apply_the_same_guardrails(monkeypatch):
    """回测不能再偷偷只打 art.pool；pool 和护栏都要能跟线上走同一条路径。"""
    from stock_selector_qlib import backtest

    seen = {}
    day = pd.Timestamp("2026-10-23")
    idx = pd.MultiIndex.from_tuples([(day, "SH600001"), (day, "SH600002")],
                                    names=["datetime", "instrument"])
    frame = pd.DataFrame({"f": [1.0, 2.0]}, index=idx)

    class Booster:
        def predict(self, X):
            return np.array([0.9, 0.8])

    class Art:
        booster = Booster()
        pool = ["SH600001"]

    def fake_features(pool, beg, end):
        seen["pool"] = list(pool)
        return frame

    monkeypatch.setattr(serving, "calendar", lambda: ["2026-10-23"])
    monkeypatch.setattr(serving, "score_pool", lambda art: ["SH600001", "SH600002"])
    monkeypatch.setattr(serving, "raw_features", fake_features)
    monkeypatch.setattr(serving, "normalize", lambda f, art: f.to_numpy())
    monkeypatch.setattr(backtest, "_guardrail_panel",
                        lambda pool, beg, end: pd.DataFrame(
                            {"SH600001": [True], "SH600002": [False]}, index=[day]))
    out = backtest.score_panel(Art(), "2026-10-23", "2026-10-23", guardrails=True)
    assert seen["pool"] == ["SH600001", "SH600002"]
    assert [inst for _, inst in out.index] == ["SH600002"]


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


# ── 当日榜内名次查询（持仓页用）：池外的票绝不给名次 ────────────────────────────

def _ranking_rows():
    """票池 2 只（art_dir 的 SH600000/SZ000001），当日只有 1 只打得分出分。"""
    return [{"code": "600000", "qlib_symbol": "SH600000", "date": "2026-10-16",
             "score_raw": 1.234, "score": 1.0, "rank": 1, "universe": 1}]


def test_ranks_by_code_never_invents_a_rank(art_dir, monkeypatch):
    """名次只能来自当天真的打过分的那批票。

    池外的持仓（基金、ST、上市太新、成交额太薄）模型从来没给它们算过，
    硬排一个「第 301 名」就是把猜测说成结论；在池里但当日没行情（停牌/断档）也一样，
    而且这一种最容易被读成「跌出榜外该卖了」，所以要把原因写进 note。
    """
    monkeypatch.setattr(artifacts, "MODEL_DIR", art_dir)
    monkeypatch.setattr(serving, "day_ranking",
                        lambda refresh=False, wait=True: _ranking_rows())
    out = serving.ranks_by_code(["600000", "000001", "510300"])
    assert (out["ready"], out["date"], out["pool_size"], out["universe"]) == (True, "2026-10-16", 2, 1)
    by_code = {it["code"]: it for it in out["items"]}
    assert by_code["600000"] == {"code": "600000", "in_pool": True, "rank": 1,
                                 "universe": 1, "score_raw": 1.234}
    # 在池里但当日没有可打分的行情 ⇒ 没名次 + 说清是"没行情"不是"排后面"
    assert by_code["000001"]["rank"] is None and by_code["000001"]["in_pool"] is True
    assert "没有可打分的行情" in by_code["000001"]["note"]
    # 池外 ⇒ 没名次 + 说清模型没给这只打过分
    assert by_code["510300"]["rank"] is None and by_code["510300"]["in_pool"] is False
    assert "不在当前 2 只候选池里" in by_code["510300"]["note"]


def test_rank_route_shape_and_failure_is_not_empty_board(art_dir, monkeypatch):
    """路由形状与后端 day_ranks 取的键一致；打分挂了要报 5xx，不能回空 items。

    空 items 到持仓页上就是「你持有的票一只都没上榜」，而真实原因是查询失败。
    """
    from fastapi.testclient import TestClient

    from stock_selector_qlib import api

    monkeypatch.setattr(artifacts, "MODEL_DIR", art_dir)
    monkeypatch.setattr(serving, "day_ranking",
                        lambda refresh=False, wait=True: _ranking_rows())
    client = TestClient(api.app)

    data = client.get("/api/selector/rank?codes=600000,510300").json()
    assert data["code"] == 0
    assert {it["code"] for it in data["data"]["items"]} == {"600000", "510300"}
    # 没给代码就是空查询，不去打分（持仓页会先判有没有票再调）
    assert client.get("/api/selector/rank").json()["data"]["items"] == []

    def _boom(refresh=False, wait=True):
        raise RuntimeError("面板里没有 2026-10-16")

    monkeypatch.setattr(serving, "day_ranking", _boom)
    assert client.get("/api/selector/rank?codes=600000").status_code == 502


def test_latest_scores_surfaces_the_blocked_original_first(monkeypatch):
    """原始第一被挡时，护栏后第一行要带上「谁被谁挡」，不能只换一只票了事。"""
    row = {"code": "600002", "qlib_symbol": "SH600002", "date": "2026-10-23",
           "score_raw": 0.8, "score": 1.0, "rank": 1, "raw_rank": 2, "universe": 10}
    monkeypatch.setattr(serving, "day_ranking", lambda refresh=False: [dict(row)])
    monkeypatch.setattr(serving, "guard_notes",
                        lambda: {"300970": {"rank_raw": 1, "reason": "薄流动"}})
    monkeypatch.setattr(serving, "_closes", lambda symbols, date: {"SH600002": 10.0})
    rows = serving.latest_scores(topk=1)
    assert rows[0]["guard_blocked_top"]["code"] == "300970"
    assert rows[0]["guard_blocked_top"]["reason"] == "薄流动"


def test_model_rows_disclose_raw_rank_and_the_blocked_original_first(monkeypatch):
    """名单第一必须能解释：护栏后第几、原始第几、原始第一被谁挡下。"""
    from stock_selector_qlib import api

    monkeypatch.setattr(api.serving, "stock_names", lambda codes: {})
    art = type("Art", (), {"run_id": "m1", "meta": {"label": "Ref($close,-10)", "train_end": "2023-12-31"}})()
    rows = [{
        "code": "600002", "qlib_symbol": "SH600002", "date": "2026-10-23",
        "score_raw": 0.8, "score": 1.0, "rank": 1, "raw_rank": 2, "universe": 10,
        "price": 10.0,
        "guard_blocked_top": {"code": "300970", "rank_raw": 1, "reason": "薄流动"},
    }]
    out = api._model_rows(art, rows)[0]
    assert "原始第 2、护栏后第 1" in out["reason"]
    assert "300970" in out["reason"] and "薄流动" in out["reason"]
    assert out["extra"]["raw_rank"] == 2
    assert out["extra"]["guard_blocked_top"]["code"] == "300970"


def test_rank_route_returns_not_ready_instead_of_blocking(art_dir, monkeypatch):
    """当日榜单没算好时，同步的名次查询**立刻**回 ready=false 并起后台预热。

    这条钉的是放开全市场之后最坏的一种坏法：冷打分要几百秒，如果它待在 HTTP 请求里，
    App/后端会超时，页面拿到的是「查询失败」或空名单，而真实情况只是「还在算」——
    空名单会被读成「持仓全跌出榜外，该卖了」。
    """
    from fastapi.testclient import TestClient

    from stock_selector_qlib import api

    monkeypatch.setattr(artifacts, "MODEL_DIR", art_dir)
    warmed = []

    def _not_ready(refresh=False, wait=True):
        assert wait is False, "名次路由必须带 wait=False，否则它又在请求里等打分"
        raise serving.RankingNotReady("2026-10-16 的全票池打分还在计算中，稍后再查名次")

    monkeypatch.setattr(serving, "ranks_by_code", _not_ready)
    monkeypatch.setattr(serving, "warm_ranking", lambda: warmed.append(1) or True)
    data = TestClient(api.app).get("/api/selector/rank?codes=600000").json()["data"]
    assert data["ready"] is False and data["items"] == [] and data["warming"] is True
    assert "还在计算中" in data["message"]
    assert warmed == [1]


def test_day_ranking_scores_once_for_concurrent_cold_requests(art_dir, monkeypatch):
    """两个请求同时撞上冷缓存只能算一遍：全市场一次要约 1 GB，各算一遍会把内存翻倍。"""
    import threading

    monkeypatch.setattr(artifacts, "MODEL_DIR", art_dir)
    monkeypatch.setattr(serving, "_cache", {})
    monkeypatch.setattr(serving, "init_qlib", lambda provider_uri=None: None)
    monkeypatch.setattr(serving, "calendar", lambda: ["2026-10-16"])
    calls = []
    entered = threading.Event()
    gate = threading.Event()

    def fake_score_frame(art, beg=None, end=None, **kw):
        calls.append(end)
        entered.set()
        gate.wait(5)
        idx = pd.MultiIndex.from_tuples([(pd.Timestamp("2026-10-16"), "SH600000")],
                                        names=["datetime", "instrument"])
        return pd.Series([1.0], index=idx), {"pool": 1, "chunk": 1, "thin_rows": 0,
                                             "no_row": 0, "scored": 1, "max_nan": 20}

    monkeypatch.setattr(serving, "score_frame", fake_score_frame)
    # 护栏要读两项行情，而这条测的是「冷缓存只算一遍」：判定注入成「谁都判不了」，
    # 名单改动那部分由 tests/test_qlib_guardrails.py 单独守。
    monkeypatch.setattr(serving, "guardrail_flags",
                        lambda date, pool: (pd.Series(dtype="bool"), pd.Series(dtype="bool")))
    first = threading.Thread(target=serving.day_ranking)
    first.start()
    entered.wait(2)                       # 第一个人已经在算（并占着 _score_lock）
    second = threading.Thread(target=serving.day_ranking)
    second.start()
    second.join(0.3)                      # 给第二个人 0.3 秒：没有单飞锁的话此刻他会再算一遍
    assert not second.is_alive() or len(calls) == 1
    assert len(calls) == 1
    gate.set()
    second.join(10)
    first.join(10)
    assert len(calls) == 1
    assert serving.day_ranking()[0]["rank"] == 1     # 缓存命中，第三次也不重算


def test_day_ranking_ties_break_by_code_not_by_panel_order(art_dir, monkeypatch):
    """同分时 #1 必须可复现：按代码升序兜底，不能取决于行情表/插入顺序。"""
    monkeypatch.setattr(artifacts, "MODEL_DIR", art_dir)
    monkeypatch.setattr(serving, "_cache", {})
    monkeypatch.setattr(serving, "init_qlib", lambda provider_uri=None: None)
    monkeypatch.setattr(serving, "calendar", lambda: ["2026-10-23"])
    day = pd.Timestamp("2026-10-23")
    idx = pd.MultiIndex.from_tuples([(day, "SH600002"), (day, "SH600001")],
                                    names=["datetime", "instrument"])

    def fake_score(art, beg=None, end=None, **kw):
        return pd.Series([0.5, 0.5], index=idx), {"pool": 2, "chunk": 1, "thin_rows": 0,
                                                   "no_row": 0, "scored": 2, "max_nan": 20}

    monkeypatch.setattr(serving, "score_frame", fake_score)
    monkeypatch.setattr(serving, "guardrail_flags",
                        lambda date, pool: (pd.Series(dtype="bool"), pd.Series(dtype="bool")))
    rows = serving.day_ranking()
    assert [r["code"] for r in rows] == ["600001", "600002"]
    assert [r["raw_rank"] for r in rows] == [1, 2]


def test_day_ranking_reports_raw_rank_after_guardrail_drops_the_original_first(art_dir, monkeypatch):
    """原始第一被挡下时，护栏后第一要留下 raw_rank=2，页面才能说清名次为什么变了。"""
    monkeypatch.setattr(artifacts, "MODEL_DIR", art_dir)
    monkeypatch.setattr(serving, "_cache", {})
    monkeypatch.setattr(serving, "init_qlib", lambda provider_uri=None: None)
    monkeypatch.setattr(serving, "calendar", lambda: ["2026-10-23"])
    day = pd.Timestamp("2026-10-23")
    idx = pd.MultiIndex.from_tuples([(day, "SH600001"), (day, "SH600002")],
                                    names=["datetime", "instrument"])

    def fake_score(art, beg=None, end=None, **kw):
        return pd.Series([0.9, 0.8], index=idx), {"pool": 2, "chunk": 1, "thin_rows": 0,
                                                   "no_row": 0, "scored": 2, "max_nan": 20}

    monkeypatch.setattr(serving, "score_frame", fake_score)
    monkeypatch.setattr(serving, "guardrail_flags",
                        lambda date, pool: (pd.Series({"SH600001": True, "SH600002": False}),
                                            pd.Series({"SH600001": False, "SH600002": False})))
    rows = serving.day_ranking()
    assert [r["code"] for r in rows] == ["600002"]
    assert rows[0]["rank"] == 1 and rows[0]["raw_rank"] == 2
    assert serving.guard_notes()["600001"]["rank_raw"] == 1


def test_warm_ranking_starts_only_one_thread(art_dir, monkeypatch):
    """预热也不能堆线程：每次未就绪的名次查询都起一个，等于把单飞锁的排队排成一排。"""
    import threading

    monkeypatch.setattr(serving, "_warming", False)
    started = threading.Event()
    release = threading.Event()

    def fake_day_ranking(refresh=False, wait=True):
        started.set()
        release.wait(5)
        return []

    monkeypatch.setattr(serving, "day_ranking", fake_day_ranking)
    assert serving.warm_ranking() is True
    started.wait(2)
    assert serving.warm_ranking() is False           # 已经有一个在跑
    release.set()
    for _ in range(200):
        if not serving._warming:
            break
        threading.Event().wait(0.02)
    assert serving._warming is False                 # 跑完必须放行，否则永远不再预热

def test_score_pool_follows_the_pool_source_switch(art_dir, tmp_path, monkeypatch):
    """放开全市场靠 QLIB_POOL_SOURCE 一个键；但名单文件缺失/为空必须是硬错，不能静默退回训练池。

    静默退回就是「以为放开了、其实还在 300 只里选」，页面和台账都看不出差别。
    """
    monkeypatch.setattr(artifacts, "MODEL_DIR", art_dir)
    art = artifacts.load()
    monkeypatch.setattr(serving, "POOL_SOURCE", "model")
    assert serving.score_pool(art) == ["SH600000", "SZ000001"]

    listing = tmp_path / "universe.txt"
    monkeypatch.setattr(serving, "UNIVERSE_FILE", listing)
    monkeypatch.setattr(serving, "POOL_SOURCE", "universe")
    with pytest.raises(RuntimeError, match="先跑 scripts/qlib_daily_universe"):
        serving.score_pool(art)
    listing.write_text("\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="空名单"):
        serving.score_pool(art)
    listing.write_text("SH600000\nSZ301165\n", encoding="utf-8")
    assert serving.score_pool(art) == ["SH600000", "SZ301165"]


def test_ranks_by_code_membership_uses_the_scored_pool(art_dir, tmp_path, monkeypatch):
    """成员判定要跟「当天真的给谁打了分」一致，不是跟训练池一致。

    universe 模式下 art.pool（训练池 300）早就不是打分名单了，拿它判会把
    「今天确实打了分」的票说成「模型没给这只打过分」，持仓页就是一堆假「不在池里」。
    """
    monkeypatch.setattr(artifacts, "MODEL_DIR", art_dir)
    listing = tmp_path / "universe.txt"
    listing.write_text("SH600000\nSZ301165\n", encoding="utf-8")
    monkeypatch.setattr(serving, "UNIVERSE_FILE", listing)
    monkeypatch.setattr(serving, "POOL_SOURCE", "universe")
    monkeypatch.setattr(serving, "day_ranking",
                        lambda refresh=False, wait=True: _ranking_rows())

    out = serving.ranks_by_code(["301165", "510300"])
    assert out["pool_size"] == 2
    by = {it["code"]: it for it in out["items"]}
    assert by["301165"]["in_pool"] is True and by["301165"]["rank"] is None
    assert "没有可打分的行情" in by["301165"]["note"]
    assert by["510300"]["in_pool"] is False
    assert "不在当前 2 只候选池里" in by["510300"]["note"]


def test_health_and_detail_report_the_live_pool_caliber(art_dir, monkeypatch):
    """G-N4：谁没进榜、为什么少，必须能从 /api/health 读到；/api/strategy/detail 的 universe 不能写死。

    放开池之后 universe 是每天变的数，报成 300 就是把口径说错。
    """
    from fastapi.testclient import TestClient

    from stock_selector_qlib import api

    monkeypatch.setattr(artifacts, "MODEL_DIR", art_dir)
    monkeypatch.setattr(serving, "POOL_SOURCE", "model")
    monkeypatch.setattr(serving, "last_score_stats", lambda: {
        "pool": 5013, "chunk": 1000, "thin_rows": 774, "no_row": 0,
        "scored": 650678, "max_nan": 20, "universe": 5004, "seconds": 550.0})
    client = TestClient(api.app)

    health = client.get("/api/health").json()
    assert health["scoring"]["thin_rows"] == 774 and health["scoring"]["universe"] == 5004
    detail = client.get("/api/strategy/detail").json()["data"][0]
    assert detail["params"]["universe"] == {"source": "model", "pool_size": 2}


# ---------------------------------------------------------------- 名单展示名

def test_display_names_come_from_the_live_quote_not_the_frozen_table(monkeypatch):
    """展示名必须问现名（腾讯快照）：Mongo bk_stocks 最近一次导入是 2026-05-19，
    拿它当首选项，「ST 摘帽/改名」在页面上永远看不出来（2026-10-23 实测当日榜内 1/30 已是旧名）。
    """
    asked: list[list[str]] = []
    stale = {"600000": "曾经的名字", "000001": "平安银行"}
    monkeypatch.setattr(serving, "_names_from_live",
                        lambda codes: {"600000": "浦发银行"})
    monkeypatch.setattr(serving, "_names_from_mongo",
                        lambda codes: (asked.append(list(codes)),
                                       {c: stale[c] for c in codes if c in stale})[1])
    got = serving.stock_names(["600000", "000001"])
    assert got["600000"] == "浦发银行"
    assert asked == [["000001"]]        # 现名答上来的那些，不该再去翻旧表


def test_a_name_the_quote_does_not_answer_falls_back_to_the_table(monkeypatch):
    """现名偶尔漏一只（停牌/接口抖动）时退回旧表：名字是展示字段，宁可用旧名也不能空着。"""
    monkeypatch.setattr(serving, "_names_from_live", lambda codes: {})
    monkeypatch.setattr(serving, "_names_from_mongo", lambda codes: {"000001": "平安银行"})
    assert serving.stock_names(["000001"]) == {"000001": "平安银行"}


def test_a_quote_outage_still_ships_names(monkeypatch):
    """整批现名取不到（网络全挂）不能把名单发成一片空名字，也不能抛出去打断选股。"""
    def boom(codes):
        raise OSError("connect timeout")

    monkeypatch.setattr(serving, "_names_from_live", boom)
    monkeypatch.setattr(serving, "_names_from_mongo", lambda codes: {"000001": "平安银行"})
    assert serving.stock_names(["000001"]) == {"000001": "平安银行"}


# ---------------------------------------------------------------------------
# 第二个策略 hot_pref（用户点名的口径：热门板块 + 新闻利好 + 排除小市值 + 趋势向上）
#
# 这一组只测**契约**：策略列表、按名字分发、行形状与分数口径。判据本身在
# tests/test_qlib_pref.py 里逐条钉过，这里不重复。
# ---------------------------------------------------------------------------

def _na_fields():
    """本引擎声明「不算」的字段清单：从插件取，别在测试里抄一份第二口径。"""
    from stock_selector_qlib import api
    return api._NA_FIELDS


def _pref_stub(topk=None, refresh=False):
    return [{
        "code": "600123", "qlib_symbol": "SH600123", "asof": "2026-10-22",
        "score": 0.87, "rank": 1, "price": 50.0,
        "reason": "热门板块「机器人」热度第 1 · 新闻利好 +5 分 · 流通市值 300 亿（≥ 50 亿闸）"
                  " · 趋势向上：站上 MA20 6.0% · 模型榜第 12 名",
        "extra": {"board_name": "机器人", "float_mv": 300.0, "model_rank": 12},
    }]


def _wire_two_strategies(art_dir, monkeypatch, *, pref=True):
    from fastapi.testclient import TestClient

    from stock_selector_qlib import api

    # 热门偏好默认下线；这组用例显式打开，别依赖进程环境
    if pref is not None:
        monkeypatch.setenv("PREF_LIST", "on" if pref else "off")
    monkeypatch.setattr(artifacts, "MODEL_DIR", art_dir)
    monkeypatch.setattr(serving, "latest_scores", lambda topk=30, refresh=False: [
        {"code": "600000", "qlib_symbol": "SH600000", "date": "2026-10-22", "score_raw": 1.234,
         "score": 0.99, "rank": 1, "price": 8.88, "universe": 2877},
    ])
    monkeypatch.setattr(serving, "latest_pref", _pref_stub)
    monkeypatch.setattr(serving, "stock_names",
                        lambda codes: {"600000": "浦发银行", "600123": "芯原股份"})
    return TestClient(api.app)


def test_策略列表里要带上热门偏好(art_dir, monkeypatch):
    client = _wire_two_strategies(art_dir, monkeypatch)
    assert client.get("/api/strategy/list").json()["data"] == ["qlib_alpha158", "hot_pref"]


def test_热门偏好默认下线(art_dir, monkeypatch):
    monkeypatch.delenv("PREF_LIST", raising=False)
    client = _wire_two_strategies(art_dir, monkeypatch, pref=None)
    assert client.get("/api/strategy/list").json()["data"] == ["qlib_alpha158"]


def test_热门偏好在PREF_LIST关掉时整条消失(art_dir, monkeypatch):
    client = _wire_two_strategies(art_dir, monkeypatch, pref=False)
    assert client.get("/api/strategy/list").json()["data"] == ["qlib_alpha158"]
    assert client.get("/api/strategy/detail").json()["code"] == 0
    assert [d["name"] for d in client.get("/api/strategy/detail").json()["data"]] == ["qlib_alpha158"]


def test_只要热门偏好时不出模型票(art_dir, monkeypatch):
    client = _wire_two_strategies(art_dir, monkeypatch)
    data = client.post("/api/selector/run", json={"strategies": ["hot_pref"]}).json()["data"]
    assert [r["code"] for r in data] == ["600123"]
    assert data[0]["strategy"] == "hot_pref"


def test_缺省请求两条都出且模型在前(art_dir, monkeypatch):
    client = _wire_two_strategies(art_dir, monkeypatch)
    data = client.post("/api/selector/run", json={}).json()["data"]
    assert [(r["code"], r["strategy"]) for r in data] == [("600000", "qlib_alpha158"),
                                                          ("600123", "hot_pref")]


def test_热门偏好行的契约与分数口径(art_dir, monkeypatch):
    client = _wire_two_strategies(art_dir, monkeypatch)
    row = client.post("/api/selector/run", json={"strategies": ["hot_pref"]}).json()["data"][0]
    for key in ("code", "name", "signal", "reason", "strategy", "asof", "price", "score",
                "rank", "extra"):
        assert key in row, f"契约键 {key} 缺失"
    assert row["name"] == "芯原股份"
    # 分数和模型榜同一个口径：0~1。百分制是展示层乘出来的，这里给两个口径会当场打架。
    assert 0.0 <= row["score"] <= 1.0
    assert row["rank"] == 1 and row["asof"] == "2026-10-22"
    # 这条榜同样不产风险分/买点/后验，必须逐行声明，不能让空白被读成「没有风险」
    assert row["extra"]["na_fields"] == list(_na_fields())
    assert row["extra"]["board_name"] == "机器人"


def test_两条策略的详情各自带说明(art_dir, monkeypatch):
    client = _wire_two_strategies(art_dir, monkeypatch)
    details = client.get("/api/strategy/detail").json()["data"]
    assert [d["name"] for d in details] == ["qlib_alpha158", "hot_pref"]
    pref_detail = details[1]
    assert "热门板块" in pref_detail["description"]
    assert pref_detail["na_fields"] == list(_na_fields())
    # 阈值不是秘密：市值线、热度滞后、新闻滞后都要在 params 里看得见
    p = pref_detail["params"]
    assert p["min_float_mv_yi"] == pref.MIN_FLOAT_MV
    assert p["weights"] == pref.WEIGHTS


def test_未知的策略名返回明确错误而不是空名单(art_dir, monkeypatch):
    client = _wire_two_strategies(art_dir, monkeypatch)
    resp = client.post("/api/selector/run", json={"strategies": ["trend_rebound"]})
    assert resp.status_code == 400 and "策略不可用" in resp.text


def test_偏好榜接线复用同一次全池打分并补上收盘价(monkeypatch):
    """`latest_pref` 是接线层：它不许再打一次分（冷算全池要 6 分钟），也不许把价格留空。"""
    monkeypatch.setattr(serving, "_cache", {})
    scored = [{"code": "600123", "qlib_symbol": "SH600123", "date": "2026-10-22",
               "score": 0.9, "rank": 3, "price": None}]
    monkeypatch.setattr(serving, "day_ranking", lambda refresh=False: scored)
    calls = []

    def fake_build(rows, day, top=None, min_float_mv=None):
        calls.append({"day": day, "top": top, "n": len(rows)})
        return pref.PrefResult(
            rows=[{"code": "600123", "qlib_symbol": "SH600123", "asof": day, "price": None}],
            stats={"in": 1, "candidates": 1, "out": 1, "no_hot_board": 0, "small_cap": 0,
                   "mv_missing": 0, "trend_down": 0, "trend_unknown": 0, "news_hit": 1,
                   "heat_day": "2026-10-21", "news_day": "2026-10-21", "data_missing": []})

    monkeypatch.setattr(pref, "build_from_rows", fake_build)
    monkeypatch.setattr(serving, "_closes", lambda syms, day: {"SH600123": 12.34})

    out = serving.latest_pref(topk=7)

    assert calls == [{"day": "2026-10-22", "top": 7, "n": 1}]
    assert out[0]["price"] == 12.34
    # 筛数与数据时点要能被 /health 读到，否则「用的是昨天的热度」这件事只有日志知道
    assert serving.last_pref_stats()["heat_day"] == "2026-10-21"
    assert serving.last_pref_stats()["out"] == 1


def test_偏好榜在全池打分为空时不去碰装载(monkeypatch):
    monkeypatch.setattr(serving, "_cache", {})
    monkeypatch.setattr(serving, "day_ranking", lambda refresh=False: [])
    monkeypatch.setattr(pref, "build_from_rows",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该跑")))
    assert serving.latest_pref() == []
    assert serving.last_pref_stats() == {}


def test_输入整路缺数时筛数要把缺的那路点出来(monkeypatch):
    """出榜 0 只可能是「今天没有票」，也可能是板块热度/新闻/市值整天没落盘——台账必须分得清。"""
    monkeypatch.setattr(serving, "_cache", {})
    scored = [{"code": "600123", "qlib_symbol": "SH600123", "date": "2026-10-22",
               "score": 0.9, "rank": 3, "price": None}]
    monkeypatch.setattr(serving, "day_ranking", lambda refresh=False: scored)

    def fake_build(rows, day, top=None, min_float_mv=None):
        return pref.PrefResult(rows=[], stats={
            "in": 1, "candidates": 0, "out": 0, "no_hot_board": 1, "small_cap": 0,
            "mv_missing": 0, "trend_down": 0, "trend_unknown": 0, "news_hit": 0,
            "heat_day": None, "news_day": "2026-10-21", "data_missing": ["板块热度"]})

    monkeypatch.setattr(pref, "build_from_rows", fake_build)
    assert serving.latest_pref() == []
    assert serving.last_pref_stats()["data_missing"] == ["板块热度"]
