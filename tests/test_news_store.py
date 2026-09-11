import pandas as pd
import pytest

from stock_selector.news.schema import ANALYSIS_SCHEMA, WATCH_SCHEMA
from stock_selector.news.store import (
    create_indexes, save_analysis_mongo, save_analysis_parquet,
    save_watch_mongo, save_watch_parquet,
)


def _analysis_row(news_id, **kw):
    row = {k: None for k in ANALYSIS_SCHEMA}
    row.update({"collect_date": "2026-09-07", "news_id": news_id,
                "title": "t", "publish_time": "2026-09-07 07:00:00"})
    row.update(kw)
    return row


def test_analysis_parquet_dedup(tmp_path):
    df1 = pd.DataFrame([_analysis_row("n1", summary="v1")])
    df2 = pd.DataFrame([_analysis_row("n1", summary="v2")])
    path1 = save_analysis_parquet(df1, "2026-09-07", tmp_path)
    save_analysis_parquet(df2, "2026-09-07", tmp_path)
    out = pd.read_parquet(path1)
    assert len(out) == 1
    assert out.iloc[0]["summary"] == "v2"


def test_analysis_parquet_aligns_schema(tmp_path):
    df = pd.DataFrame([{"collect_date": "2026-09-07", "news_id": "n1"}])
    path = save_analysis_parquet(df, "2026-09-07", tmp_path)
    assert list(pd.read_parquet(path).columns) == ANALYSIS_SCHEMA


def test_watch_parquet_overwrite(tmp_path):
    d1 = pd.DataFrame([{**{k: None for k in WATCH_SCHEMA},
                        "collect_date": "2026-09-07", "code": "BK0464",
                        "direction": "bullish", "source_news_ids": ["n1"]}])
    d2 = pd.DataFrame([{**{k: None for k in WATCH_SCHEMA},
                        "collect_date": "2026-09-07", "code": "BK0464",
                        "direction": "bullish", "source_news_ids": ["n1", "n2"]}])
    path = save_watch_parquet(d1, "2026-09-07", tmp_path)
    save_watch_parquet(d2, "2026-09-07", tmp_path)
    out = pd.read_parquet(path)
    assert len(out) == 1 and list(out.iloc[0]["source_news_ids"]) == ["n1", "n2"]


def _mongo_available():
    try:
        import pymongo
        c = pymongo.MongoClient("127.0.0.1", 27017, serverSelectionTimeoutMS=1500)
        c.admin.command("ping")
        c.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _mongo_available(), reason="local mongo unavailable")


class TestStoreMongo:
    def setup_method(self):
        import pymongo
        client = pymongo.MongoClient("127.0.0.1", 27017, serverSelectionTimeoutMS=1500)
        self.db = client["eastmoney_news"]
        self.client = client
        self.db["news_analysis"].delete_many({"collect_date": {"$in": ["2026-09-06", "2026-09-07"]}})
        self.db["watch_target"].delete_many({"collect_date": {"$in": ["2026-09-06", "2026-09-07"]}})

    def teardown_method(self):
        self.client.close()

    def test_analysis_mongo_rebuild(self):
        df = pd.DataFrame([_analysis_row("n1", summary="v1")])
        save_analysis_mongo(self.db, df, "2026-09-07")
        assert self.db["news_analysis"].find_one({"_id": "2026-09-07|n1"})["summary"] == "v1"
        df2 = pd.DataFrame([_analysis_row("n1", summary="v2")])
        save_analysis_mongo(self.db, df2, "2026-09-07")  # 重写
        assert self.db["news_analysis"].count_documents({"collect_date": "2026-09-07"}) == 1
        assert self.db["news_analysis"].find_one({"_id": "2026-09-07|n1"})["summary"] == "v2"

    def test_watch_mongo_day_rebuild(self):
        docs = [{"collect_date": "2026-09-07", "code": "BK0464", "name": "",
                 "category": "板块", "direction": "bullish", "score": 8,
                 "priority": "", "reason": "r", "source_news_ids": ["n1"],
                 "llm_model": "m"}]
        self.db["watch_target"].insert_one({"collect_date": "2026-09-06",
                                            "code": "OLD", "name": "", "category": "板块",
                                            "direction": "bullish", "score": 1,
                                            "priority": "", "reason": "",
                                            "source_news_ids": ["old1"], "llm_model": "m"})
        save_watch_mongo(self.db, docs, "2026-09-07")
        assert self.db["watch_target"].count_documents({"collect_date": "2026-09-07"}) == 1
        doc = self.db["watch_target"].find_one({"collect_date": "2026-09-07"})
        assert len(doc["_id"]) == 16 and doc["_id"] == doc["_id"].lower()
        assert self.db["watch_target"].count_documents({"collect_date": "2026-09-06"}) == 1

    def test_indexes(self):
        create_indexes(self.db)
        news_indexes = [i["name"] for i in self.db["news_analysis"].list_indexes()]
        watch_indexes = [i["name"] for i in self.db["watch_target"].list_indexes()]
        assert "collect_date_1" in news_indexes and "news_id_1" in news_indexes
        assert "code_1" in watch_indexes and "collect_date_1_code_1" in watch_indexes
