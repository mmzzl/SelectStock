"""tests/test_news_read.py — 消息面只读层：退到最近批次、整形风险行、健康位。"""
from datetime import date

from stock_selector.news.read import daily, health, latest_day


class _Col:
    def __init__(self, docs):
        self.docs = docs

    def distinct(self, field, query=None):
        want = (query or {}).get(field, {}).get("$in", [])
        return [d[field] for d in self.docs if d[field] in want]

    def find(self, query=None, projection=None):
        query = query or {}
        day = query.get("collect_date")
        if isinstance(day, dict):
            days = day.get("$in", [])
            return [d for d in self.docs if d.get("collect_date") in days]
        return [d for d in self.docs if d.get("collect_date") == day]


class _Db:
    def __init__(self, watch, analysis):
        self.watch_target = _Col(watch)
        self.news_analysis = _Col(analysis)


def _watch(day, name, direction, score, **kw):
    return {"collect_date": day, "code": kw.get("code", ""), "name": name,
            "category": kw.get("category", "个股"), "direction": direction, "score": score,
            "priority": kw.get("priority", ""), "reason": kw.get("reason", ""),
            "source_news_ids": kw.get("src", ["n1"])}


TODAY = date(2026, 10, 13)


def test_latest_day_retries_to_newest_batch_with_data():
    """分析在 22:00 跑、盖的是当天，早上按「今天」查是空的——必须退到最近一批。"""
    db = _Db([_watch("2026-10-11", "旧批次", "bearish", -3),
              _watch("2026-10-12", "上一批", "bearish", -3)], [])
    assert latest_day(db, TODAY, lookback=4) == "2026-10-12"
    assert latest_day(db, TODAY, lookback=0) is None


def test_risk_row_is_shaped_into_one_line():
    """风险行存的是「desc 前 20 字当 name + 整句当 reason」，原样透出就是半句标题重复一遍全文。"""
    db = _Db([_watch("2026-10-13", "IPO欺诈调查可能导致茶百道上市失败或严", "risk", -8,
                     category="风险", reason="IPO欺诈调查可能导致茶百道上市失败或严重推迟")], [])
    item = daily(db, today=TODAY)["items"]["risk"][0]
    assert item["name"] == "IPO欺诈调查可能导致茶百道上市失败或严重推迟"
    assert item["reason"] == "" and item["category"] == ""


def test_daily_sorts_bearish_by_severity_and_bullish_by_strength():
    db = _Db([
        _watch("2026-10-13", "轻利空", "bearish", -2),
        _watch("2026-10-13", "重利空", "bearish", -8, src=["n1", "n2"]),
        _watch("2026-10-13", "强利好", "bullish", 9),
        _watch("2026-10-13", "弱利好", "bullish", 3),
        _watch("2026-10-13", "低优先关注", "watch", 0, priority="低"),
        _watch("2026-10-13", "高优先关注", "watch", 0, priority="高"),
    ], [])
    out = daily(db, today=TODAY)
    assert [i["name"] for i in out["items"]["bearish"]] == ["重利空", "轻利空"]
    assert [i["name"] for i in out["items"]["bullish"]] == ["强利好", "弱利好"]
    assert [i["name"] for i in out["items"]["watch"]] == ["高优先关注", "低优先关注"]
    assert out["counts"] == {"bullish": 2, "bearish": 2, "risk": 0, "watch": 2}
    assert out["stale_days"] == 0
    assert out["items"]["bearish"][0]["news_count"] == 2


def test_health_counts_silent_failures():
    """LLM 失败行 raw 为空、结论里写着「分析失败」——健康位要把它算出来。"""
    db = _Db([], [
        {"collect_date": "2026-10-13", "raw": None, "conclusion": "分析失败，LLM 原始输出: None"},
        {"collect_date": "2026-10-13", "raw": "", "conclusion": "分析失败"},
        {"collect_date": "2026-10-13", "raw": "{...}", "conclusion": "正常"},
    ])
    h = health(db, "2026-10-13", TODAY)
    assert h == {"analyzed": 3, "failed": 2, "fail_rate": 0.6667}


def test_daily_without_batch_still_reports_health():
    db = _Db([], [{"collect_date": "2026-10-13", "raw": None, "conclusion": "分析失败"}])
    out = daily(db, today=TODAY)
    assert out["day"] is None and out["counts"]["bearish"] == 0
    assert out["health"]["failed"] == 1
