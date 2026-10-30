"""8002 薄适配层：资金榜与原始消息流的路由将内核异常映射成 code，不能变成 500。

只测这一层的映射（内核算法在 test_fund_rank_read.py / test_news_feed.py 里测）：
前端按 code 分支渲染空态与错误文案，所以 code:1（缺产物）/ code:2（参数非法）/ code:3（没这条）
必须各归各位。
"""

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from stock_selector import api
from stock_selector.fundflow import rank as rank_mod

CLIENT = TestClient(api.app)


def _seed_rank(tmp_path, monkeypatch):
    import json

    (tmp_path / "rank.parquet").parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([
        {"window": 3, "code": "600000", "name": "浦发银行", "close": 9.0,
         "net_inflow": 3.0e8, "turnover": 2.0e9, "ratio": 0.15, "ret_pct": 4.0, "days": 3},
        {"window": 3, "code": "000001", "name": "平安银行", "close": 11.0,
         "net_inflow": -2.0e8, "turnover": 1.0e9, "ratio": -0.2, "ret_pct": -3.0, "days": 3},
    ]).to_parquet(tmp_path / "rank.parquet", index=False)
    (tmp_path / "meta.json").write_text(json.dumps(
        {"as_of": "2026-10-29", "windows": [3], "days_used": ["2026-10-29"],
         "counts_by_window": {"3": 2}, "thin_days": []}), encoding="utf-8")
    monkeypatch.setattr(rank_mod, "RANK_DIR", tmp_path)


@pytest.fixture
def client_no_side_effects(monkeypatch):
    """路由会读磁盘产物：每次清掉按 mtime 键控的缓存，避免测试间串数据。"""
    rank_mod._cache.clear()
    yield
    rank_mod._cache.clear()


def test_fund_rank_route_returns_rows(monkeypatch, tmp_path, client_no_side_effects):
    _seed_rank(tmp_path, monkeypatch)
    body = CLIENT.get("/api/fund/rank?window=3&side=in&limit=10").json()
    assert body["code"] == 0
    assert body["data"]["asof"] == "2026-10-29"
    assert [r["code"] for r in body["data"]["rows"]] == ["600000", "000001"]


def test_fund_rank_route_maps_missing_product_to_code_1(monkeypatch, tmp_path, client_no_side_effects):
    monkeypatch.setattr(rank_mod, "RANK_DIR", tmp_path / "nothing")
    body = CLIENT.get("/api/fund/rank").json()
    assert body["code"] == 1
    assert "build_fund_rank.py" in body["message"]


def test_fund_rank_route_maps_illegal_window_to_code_2(monkeypatch, tmp_path, client_no_side_effects):
    _seed_rank(tmp_path, monkeypatch)
    body = CLIENT.get("/api/fund/rank?window=5").json()
    assert body["code"] == 2
    assert "window" in body["message"]


def test_news_feed_route_returns_empty_when_mongo_is_down(monkeypatch):
    monkeypatch.setattr(api.news_read, "connect", lambda: None)
    body = CLIENT.get("/api/news/feed").json()
    assert body["code"] == 1
    assert body["data"] is None


def test_news_feed_route_maps_bad_cursor_to_code_2(monkeypatch):
    import mongomock

    monkeypatch.setattr(api.news_read, "connect", lambda: mongomock.MongoClient()["eastmoney_news"])
    body = CLIENT.get("/api/news/feed?before=%20zz").json()
    assert body["code"] == 2


def test_news_item_route_maps_unknown_id_to_code_3(monkeypatch):
    import mongomock

    db = mongomock.MongoClient()["eastmoney_news"]
    monkeypatch.setattr(api.news_read, "connect", lambda: db)
    body = CLIENT.get("/api/news/item?id=6ae3ef969ca5d4a7d72421b8").json()
    assert body["code"] == 3
