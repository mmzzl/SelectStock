"""从 MongoDB 读取东财新闻（真实实现）。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from stock_selector.news.normalize import normalize_secid
from stock_selector.news.names import enrich_hints

logger = logging.getLogger("news.fetch")


def natural_day_start(days: int, today: datetime | None = None) -> str:
    """自然日口径：返回 (今天 - N天) 的 00:00:00。"""
    today = today or datetime.now()
    return (today - timedelta(days=days)).strftime("%Y-%m-%d 00:00:00")


def build_query(days: int) -> dict:
    return {"showTime": {"$gte": natural_day_start(days)}} if days > 0 else {}


def news_to_item(d: dict, name_maps=None) -> dict:
    """把 mongo 文档映射为分析输入字段。"""
    hints = [normalize_secid(x) for x in (d.get("stockList") or [])]
    if name_maps:
        board_map, stock_map = name_maps
        hints = enrich_hints(hints, board_map, stock_map)
    return {
        "news_id": str(d.get("_id")),
        "title": d.get("title") or "",
        "summary": d.get("summary") or "",
        "source": "eastmoney",
        "url": "",
        "publish_time": str(d.get("showTime") or ""),
        "hints": hints,
    }


def fetch_news(col, days: int, limit: int, name_maps=None) -> list[dict]:
    """从 news 集合读取新闻（col 可注入，便于 fake 测试）。"""
    cursor = col.find(build_query(days)).sort("_id", -1)
    if limit:
        cursor = cursor.limit(limit)
    return [news_to_item(d, name_maps) for d in cursor]
