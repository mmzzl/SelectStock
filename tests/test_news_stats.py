import pandas as pd
import pytest

from stock_selector.news.schema import WATCH_SCHEMA
from stock_selector.news.stats import (
    load_watch_range, rank_heat, rank_heat_mongo, trend, trend_mongo,
)


def _watch(date, code, direction, score, names=("n1",), category="板块"):
    return {k: None for k in WATCH_SCHEMA} | {
        "collect_date": date, "code": code, "name": code, "category": category,
        "direction": direction, "score": score, "source_news_ids": list(names),
    }


def _save_watch(tmp_path, date, rows):
    df = pd.DataFrame(rows)
    df.to_parquet(tmp_path / f"{date}.parquet", index=False, compression="zstd")


def test_load_watch_range(tmp_path):
    _save_watch(tmp_path, "2026-09-07", [_watch("2026-09-07", "BK0464", "bullish", 8)])
    _save_watch(tmp_path, "2026-09-06", [_watch("2026-09-06", "BK0464", "bullish", 3)])
    _save_watch(tmp_path, "2026-09-05", [_watch("2026-09-05", "BK0464", "bullish", 1)])
    df = load_watch_range(tmp_path, "2026-09-06", "2026-09-08")
    assert set(df["collect_date"]) == {"2026-09-06", "2026-09-07"}


def test_rank_heat_aggregation():
    df = pd.DataFrame([
        _watch("2026-09-01", "BK0464", "bullish", 8, ("n1", "n2")),
        _watch("2026-09-02", "BK0464", "bullish", 3, ("n3",)),
        _watch("2026-09-02", "BK0464", "bearish", -5, ("n4",)),
        _watch("2026-09-02", "300207", "bullish", 6, ("n3",)),
    ])
    out = rank_heat(df)
    row = out.set_index("code").loc["BK0464"]
    assert row["news_count"] == 3  # {n1,n2} ∪ {n3} = 3
    assert row["total_score"] == 11  # 8 + 3


def test_rank_heat_category_filter():
    df = pd.DataFrame([
        _watch("2026-09-01", "BK0464", "bullish", 8),
        _watch("2026-09-01", "300207", "bullish", 6, category="个股"),
    ])
    out = rank_heat(df, category="板块")
    assert set(out["code"]) == {"BK0464"}


def test_rank_heat_topn():
    df = pd.DataFrame([
        _watch("2026-09-01", "A", "bullish", 1),
        _watch("2026-09-01", "B", "bullish", 9),
    ])
    out = rank_heat(df, top_n=1)
    assert list(out["code"]) == ["B"]


def test_trend_cumsum():
    df = pd.DataFrame([
        _watch("2026-09-01", "BK0464", "bullish", 8),
        _watch("2026-09-02", "BK0464", "bearish", -3),
    ])
    out = trend(df, "BK0464")
    assert out["score"].tolist() == [8, -3]
    assert out["cum"].tolist() == [8, 5]


def test_empty_input():
    empty = pd.DataFrame(columns=WATCH_SCHEMA)
    assert rank_heat(empty).empty
    assert trend(empty, "BK0464").empty
    assert load_watch_range("/nonexistent", "2026-01-01", "2026-12-31").empty


def _mongo_available():
    try:
        import pymongo
        c = pymongo.MongoClient("127.0.0.1", 27017, serverSelectionTimeoutMS=1500)
        c.admin.command("ping")
        c.close()
        return True
    except Exception:
        return False


class TestStatsMongo:
    @pytest.fixture(autouse=True)
    def _db(self):
        if not _mongo_available():
            pytest.skip("local mongo unavailable")
        import pymongo
        client = pymongo.MongoClient("127.0.0.1", 27017, serverSelectionTimeoutMS=1500)
        db = client["eastmoney_news"]
        db["watch_target"].delete_many({"collect_date": {"$gte": "2026-09-01",
                                                         "$lt": "2026-09-08"}})
        docs = [_watch("2026-09-01", "BK0464", "bullish", 8, ("n1",)),
                _watch("2026-09-02", "BK0464", "bullish", 3, ("n2",))]
        for d in docs:
            db["watch_target"].insert_one(d)
        yield db["watch_target"]
        db["watch_target"].delete_many({"collect_date": {"$gte": "2026-09-01",
                                                         "$lt": "2026-09-08"}})
        client.close()

    def test_rank_heat_mongo(self, _db):
        rows = rank_heat_mongo(_db, "2026-09-01", "2026-09-08", top_n=10)
        assert rows and rows[0]["_id"] == "BK0464"
        assert rows[0]["total_score"] == 11 and rows[0]["news_count"] == 2

    def test_trend_mongo(self, _db):
        rows = trend_mongo(_db, "BK0464", "2026-09-01", "2026-09-08")
        assert rows[-1]["cum"] == 11
