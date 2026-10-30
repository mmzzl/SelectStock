"""tests/test_news_feed.py — 原始消息流只读层：倒序分页、游标不漏不重、详情字段整形。

为什么单测这些：这一层直接对着 mongo 的 `news` 集合（每 30 秒在长），App 上是用户看到的
「实时消息」。分页游标用错字段（按 crawlTime 排、按 showTime 过滤）会安静地重复或跳条；
非法 id 不校验会直接 500。
"""

import pytest
from bson import ObjectId

from stock_selector.news.read import feed, item

try:
    import mongomock
except ImportError:  # pragma: no cover
    pytest.skip("需要 mongomock", allow_module_level=True)


def _db(*docs):
    db = mongomock.MongoClient()["eastmoney_news"]
    for i, (title, show) in enumerate(docs):
        db.news.insert_one({
            "_id": ObjectId(), "code": f"2026103{i}", "title": title, "summary": f"{title} 的正文摘要",
            "showTime": show, "stockList": ["90.BK0800", "0.161631"],
            "pinglun_Num": i, "share": i * 2, "crawlTime": "2026-10-30 09:15:02",
            "image": [], "realSort": "1793322808028590", "titleColor": 3,
        })
    return db


DB = _db(("早盘要闻", "2026-10-30 09:00:00"), ("资金动向", "2026-10-30 09:30:00"),
         ("盘中快讯", "2026-10-30 10:00:00"), ("午后题材", "2026-10-30 13:00:00"),
         ("尾盘异动", "2026-10-30 14:30:00"))


# ---------------------------------------------------------------- 列表

def test_feed_is_newest_first_and_only_carries_display_fields():
    out = feed(DB, limit=3)
    assert [r["title"] for r in out["rows"]] == ["尾盘异动", "午后题材", "盘中快讯"]
    assert set(out["rows"][0]) == {"id", "title", "summary", "showTime", "stockList"}


def test_feed_cursor_returns_older_items_without_gaps_or_repeats():
    """游标是 _id（唯一且有序）。按时间字符串翻页遇到同秒会重条或跳条。"""
    first = feed(DB, limit=2)
    second = feed(DB, limit=2, before=first["next_cursor"])
    assert [r["title"] for r in second["rows"]] == ["盘中快讯", "资金动向"]
    assert {r["id"] for r in first["rows"]} & {r["id"] for r in second["rows"]} == set()


def test_feed_limit_is_clamped():
    assert len(feed(DB, limit=9999)["rows"]) == 5


def test_feed_rejects_a_non_hex_cursor():
    with pytest.raises(ValueError):
        feed(DB, before="not-an-objectid")


def test_feed_on_empty_collection_returns_empty_rows():
    out = feed(mongomock.MongoClient()["eastmoney_news"], limit=10)
    assert out["rows"] == [] and out["next_cursor"] is None


# ---------------------------------------------------------------- 详情

def test_item_returns_full_detail_for_that_document():
    doc_id = feed(DB, limit=1)["rows"][0]["id"]
    out = item(DB, doc_id)
    assert out["title"] == "尾盘异动"
    assert out["summary"] == "尾盘异动 的正文摘要"
    assert out["showTime"] == "2026-10-30 14:30:00"
    assert out["stockList"] == ["90.BK0800", "0.161631"]
    assert (out["pinglun_Num"], out["share"]) == (4, 8)


def test_item_missing_id_is_none_not_an_error():
    assert item(DB, str(ObjectId())) is None


def test_item_rejects_a_non_hex_id():
    with pytest.raises(ValueError):
        item(DB, "../../etc/passwd")
