from datetime import datetime

from stock_selector.news.fetch import build_query, natural_day_start, fetch_news


class FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, key, direction):
        self._docs = sorted(self._docs, key=lambda d: d.get(key), reverse=True)
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    def __iter__(self):
        return iter(self._docs)


class FakeCol:
    def __init__(self, docs):
        self.docs = docs

    def find(self, query):
        if not query:
            return FakeCursor(list(self.docs))
        gte = query["showTime"]["$gte"]
        return FakeCursor([d for d in self.docs if d.get("showTime") >= gte])


def _news(**kw):
    doc = {"_id": "69b34b434569bcac902bd6f0",
           "title": "理想入股欣旺达动力", "summary": "9月4日晚间公告...",
           "showTime": "2026-09-06 07:08:00", "stockList": ["90.BK0464", "0.159945"]}
    doc.update(kw)
    return doc


def test_natural_day_start():
    assert natural_day_start(1, datetime(2026, 9, 7)) == "2026-09-06 00:00:00"
    assert natural_day_start(3, datetime(2026, 9, 7)) == "2026-09-04 00:00:00"


def test_build_query_no_filter():
    assert build_query(0) == {}


def test_news_to_item_maps_fields():
    item = fetch_news(FakeCol([_news()]), days=0, limit=50)[0]
    assert item["news_id"] == "69b34b434569bcac902bd6f0"
    assert item["summary"] == "9月4日晚间公告..."
    assert item["publish_time"] == "2026-09-06 07:08:00"
    assert item["source"] == "eastmoney" and item["url"] == ""
    assert item["hints"][0]["code"] == "BK0464"
    assert item["hints"][1]["code"] == "159945"


def test_news_to_item_enriches_names():
    item = fetch_news(FakeCol([_news()]), days=0, limit=50,
                      name_maps=({"BK0464": "汽车整车"}, {"159945": "XX股份"}))[0]
    assert item["hints"][0]["name"] == "汽车整车"
    assert item["hints"][1]["name"] == "XX股份"


def test_news_to_item_missing_fields():
    item = fetch_news(FakeCol([_news(stockList=None)]), days=0, limit=50)[0]
    assert item["hints"] == [] and item["url"] == ""


def test_fetch_news_limit_and_filter():
    yesterday = natural_day_start(1)
    threshold = natural_day_start(0)  # 今天 00:00:00
    day_after = "2099-01-01 00:00:00"
    docs = [
        _news(_id="1", showTime=yesterday),          # < 阈值（被过滤）
        _news(_id="2", showTime=yesterday),          # < 阈值（被过滤）
        _news(_id="3", showTime=threshold),          # >=
        _news(_id="4", showTime=threshold),          # >=
        _news(_id="5", showTime=day_after),          # >=（最新）
    ]
    items = fetch_news(FakeCol(docs), days=1, limit=2)
    assert len(items) == 2
    assert all(i["publish_time"] >= threshold for i in items)
