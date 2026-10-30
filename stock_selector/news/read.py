"""news/read.py — 消息面结果的只读层（8002 接口与每日摘要共用）。

为什么要有 `latest_day`：分析任务在盘后 22:00 跑，`collect_date` 盖的是**跑任务那天**，
而它拆出来的消息面服务的是**第二天早上**。所以按「今天」查永远是空的（这正是过去
盘中基本面腿一条都没命中的原因）。读取层一律先退到最近一个有数据的日子，再由调用方
决定「旧了要不要提示」。

这里只读不算：分数、方向、理由全部由 LLM 那层产出，本模块只做筛选、排序与计数。
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

logger = logging.getLogger("news.read")

DIRECTIONS = ("bullish", "bearish", "risk", "watch")
_WATCH_PRIORITY = {"高": 0, "中": 1, "低": 2}


def connect():
    """连 config.yaml 里的 mongo；连不上返回 None（调用方出空态，不抛）。"""
    try:
        from pymongo import MongoClient

        from ..datasource.mongo_source import _load_mongo_config

        mongo = _load_mongo_config()
        client = MongoClient(host=mongo.get("host", "127.0.0.1"),
                             port=int(mongo.get("port", 27017)),
                             serverSelectionTimeoutMS=3000)
        client.admin.command("ping")
        return client[mongo.get("database", "eastmoney_news")]
    except Exception as exc:  # noqa: BLE001
        logger.warning("mongo 不可用，消息面为空: %s", exc)
        return None


def _days_back(today: date, lookback: int) -> list[str]:
    return [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(lookback + 1)]


def latest_day(db, today: date | None = None, lookback: int = 4) -> str | None:
    """最近一个有 watch_target 的自然日（含今天，往回最多 lookback 天）。"""
    today = today or date.today()
    days = _days_back(today, lookback)
    have = set()
    for d in db.watch_target.distinct("collect_date", {"collect_date": {"$in": days}}):
        if isinstance(d, str):
            have.add(d)
    for d in days:
        if d in have:
            return d
    return None


def _days_ago(day: str, today: date) -> int:
    try:
        return (today - datetime.strptime(day, "%Y-%m-%d").date()).days
    except (TypeError, ValueError):
        return -1


def _doc_to_item(d: dict) -> dict:
    direction = d.get("direction") or ""
    name = d.get("name") or ""
    reason = d.get("reason") or ""
    category = d.get("category") or ""
    if direction == "risk":
        # 风险行存的是 desc 前 20 字当 name、整句当 reason（targets.py:_risk_to_target），
        # 原样透出会让 App 和推送各显示一遍「半句标题 + 同一句全文」；这里一次整形。
        name, reason, category = (reason or name), "", ""
    return {
        "code": d.get("code") or "",
        "name": name,
        "category": category,
        "direction": direction,
        "score": int(d.get("score") or 0),
        "priority": d.get("priority") or "",
        "reason": reason,
        "news_count": len(d.get("source_news_ids") or []),
    }


def _sorted(direction: str, rows: list[dict]) -> list[dict]:
    """利空/风险按强度从重到轻（分数最负在前），利好反过来；关注名单先按优先级。"""
    if direction == "watch":
        return sorted(rows, key=lambda r: (_WATCH_PRIORITY.get(r["priority"], 3), -abs(r["score"])))
    reverse = direction == "bullish"
    return sorted(rows, key=lambda r: r["score"], reverse=reverse)


def daily(db, day: str | None = None, today: date | None = None,
          lookback: int = 4, limit: int = 60) -> dict:
    """某日的消息面全貌：计数 + 四类清单 + 数据健康位。"""
    today = today or date.today()
    day = day or latest_day(db, today, lookback)
    if not day:
        return {"day": None, "stale_days": None, "counts": {d: 0 for d in DIRECTIONS},
                "items": {d: [] for d in DIRECTIONS}, "health": health(db, None, today)}
    rows = [_doc_to_item(d) for d in db.watch_target.find({"collect_date": day})]
    grouped = {d: [r for r in rows if r["direction"] == d] for d in DIRECTIONS}
    return {
        "day": day,
        "stale_days": _days_ago(day, today),
        "counts": {d: len(grouped[d]) for d in DIRECTIONS},
        "items": {d: _sorted(d, grouped[d])[:limit] for d in DIRECTIONS},
        "health": health(db, day, today),
    }


def health(db, day: str | None, today: date | None = None) -> dict:
    """分析这一批新闻时 LLM 到底成功了没有——静默全失败过一次，不能再让它无声无息。"""
    today = today or date.today()
    days = [day] if day else _days_back(today, 4)
    rows = list(db.news_analysis.find(
        {"collect_date": {"$in": [d for d in days if d]}},
        {"raw": 1, "conclusion": 1}))
    if not rows:
        return {"analyzed": 0, "failed": 0, "fail_rate": None}
    failed = [r for r in rows if not r.get("raw") or "分析失败" in str(r.get("conclusion") or "")]
    return {"analyzed": len(rows), "failed": len(failed),
            "fail_rate": round(len(failed) / len(rows), 4)}


def holdings_hits(db, day: str, codes: list[str]) -> list[dict]:
    """当日与这些代码相关的消息面（持仓相关性由调用方给代码，这里只做匹配）。"""
    if not day or not codes:
        return []
    rows = [_doc_to_item(d) for d in db.watch_target.find(
        {"collect_date": day, "code": {"$in": list(codes)}})]
    return _sorted("bearish", rows)


# ---------------------------------------------------------------- 原始消息流（App 新闻详情页）

FEED_MAX_LIMIT = 100
_FEED_FIELDS = ("title", "summary", "showTime", "stockList")
_ID_RE = re.compile(r"^[0-9a-fA-F]{24}$")


def _oid(value):
    """游标/详情 id 换成 ObjectId。非法值直接 ValueError——路由层出 code:2，不能让它 500。"""
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    if not _ID_RE.match(text):
        raise ValueError("新闻 id 需为 24 位十六进制的 _id")
    from bson import ObjectId

    return ObjectId(text)


def _feed_doc(d: dict) -> dict:
    return {
        "id": str(d["_id"]),
        "title": d.get("title") or "",
        "summary": d.get("summary") or "",
        "showTime": d.get("showTime") or "",
        "stockList": [str(x) for x in (d.get("stockList") or [])],
    }


def feed(db, limit: int = 30, before: str | None = None) -> dict:
    """原始消息流倒序一页。

    游标用 `_id` 不用 `showTime`：`_id` 唯一且与采集顺序同向（实测 15,137 条全部是
    ObjectId、升序与 showTime 升序一致），按时间字符串翻页会在同一秒撞车时重条或漏条。
    """
    limit = max(1, min(int(limit), FEED_MAX_LIMIT))
    cursor = _oid(before)
    query = {"_id": {"$lt": cursor}} if cursor is not None else {}
    docs = list(db.news.find(query, {f: 1 for f in _FEED_FIELDS})
                .sort([("_id", -1)]).limit(limit))
    rows = [_feed_doc(d) for d in docs]
    return {"rows": rows, "limit": limit,
            "next_cursor": rows[-1]["id"] if rows else None}


def item(db, news_id: str) -> dict | None:
    """一条原始消息：标题 + 摘要 + 关联标的 + 互动数。找不到返回 None（调用方出空态）。"""
    doc = db.news.find_one({"_id": _oid(news_id)})
    if not doc:
        return None
    return {**_feed_doc(doc), "pinglun_Num": int(doc.get("pinglun_Num") or 0),
            "share": int(doc.get("share") or 0)}

