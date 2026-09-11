"""两层存储的列定义。"""

from __future__ import annotations

ANALYSIS_SCHEMA = [
    "collect_date", "news_id", "title", "source", "url", "publish_time",
    "content_excerpt", "summary", "sentiment", "sentiment_score",
    "impact_period", "bullish", "bearish", "risks", "watchlist",
    "follow_signals", "action", "key_points", "market_reaction",
    "conclusion", "llm_model", "raw",
    "stock_hints",
]

WATCH_SCHEMA = [
    "collect_date", "code", "name", "category", "direction", "score",
    "priority", "reason", "source_news_ids", "llm_model",
]
