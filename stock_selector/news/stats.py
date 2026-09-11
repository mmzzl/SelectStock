"""跨天统计：热度排行 + 个股/板块趋势（方案A 惰性聚合）。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from stock_selector.news.schema import WATCH_SCHEMA


def load_watch_range(watch_dir: str | Path, start: str, end: str) -> pd.DataFrame:
    """近 N 天 watch parquet 惰性 concat（文件名 YYYY-MM-DD，按 [start, end) 过滤）。"""
    frames = []
    for p in sorted(Path(watch_dir).glob("*.parquet")):
        d = p.stem
        if start <= d < end:
            frames.append(pd.read_parquet(p))
    if not frames:
        return pd.DataFrame(columns=WATCH_SCHEMA)
    return pd.concat(frames, ignore_index=True)


def rank_heat(df, category=None, direction="bullish", top_n=30) -> pd.DataFrame:
    """热度排行：按 code 聚合，news_count=source_news_ids 去重总数，total_score=sum。"""
    cond = df["direction"] == direction
    if category:
        cond &= df["category"] == category
    part = df[cond]
    if part.empty:
        return pd.DataFrame(columns=["code", "name", "news_count", "total_score"])
    agg = part.groupby("code").agg(
        name=("name", "first"),
        news_count=("source_news_ids", lambda s: len(set().union(*s))),
        total_score=("score", "sum"),
    ).sort_values("total_score", ascending=False).head(top_n)
    return agg.reset_index()


def trend(df, code) -> pd.DataFrame:
    """个股/板块趋势：逐日 score sum + cumsum。"""
    part = df[df["code"] == code]
    if part.empty:
        return pd.DataFrame(columns=["collect_date", "score", "cum"])
    out = part.groupby("collect_date")["score"].sum().sort_index().reset_index()
    out["cum"] = out["score"].cumsum()
    return out


def rank_heat_mongo(col, start, end, category=None, direction="bullish", top_n=30) -> list:
    """Mongo 端热度排行。"""
    match = {"collect_date": {"$gte": start, "$lt": end}, "direction": direction}
    if category:
        match["category"] = category
    return list(col.aggregate([
        {"$match": match},
        {"$group": {"_id": "$code",
                    "name": {"$first": "$name"},
                    "news_count": {"$sum": {"$size": "$source_news_ids"}},
                    "total_score": {"$sum": "$score"}}},
        {"$sort": {"total_score": -1}},
        {"$limit": top_n},
    ]))


def trend_mongo(col, code, start, end) -> list:
    """Mongo 端逐日净分 + 应用端 cumsum。"""
    rows = list(col.aggregate([
        {"$match": {"code": code, "collect_date": {"$gte": start, "$lt": end}}},
        {"$group": {"_id": "$collect_date", "score": {"$sum": "$score"}}},
        {"$sort": {"_id": 1}},
    ]))
    cum = 0
    out = []
    for r in rows:
        cum += r["score"]
        out.append({"collect_date": r["_id"], "score": r["score"], "cum": cum})
    return out
