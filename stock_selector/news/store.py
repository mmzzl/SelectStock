"""两层结果的 parquet / Mongo 持久化（parquet 为准，Mongo 为查询副本）。"""

from __future__ import annotations

import logging
from hashlib import md5
from pathlib import Path

import pandas as pd

from stock_selector.news.schema import ANALYSIS_SCHEMA, WATCH_SCHEMA

logger = logging.getLogger("news.store")


def _align(df, schema):
    df = df.copy()
    for col in schema:
        if col not in df.columns:
            df[col] = None
    return df[schema]


def _normalize_collect_date(df):
    """统一 collect_date 为 YYYY-MM-DD 字符串（兼容旧 parquet 的 datetime 类型）。"""
    if "collect_date" in df.columns:
        df["collect_date"] = df["collect_date"].map(
            lambda v: v.strftime("%Y-%m-%d") if isinstance(v, pd.Timestamp) else v)
    return df


def save_analysis_parquet(df, collect_date, output_dir: str | Path) -> Path:
    """news_analysis 增量合并（按 news_id 去重保留最新），zstd 落盘。"""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{collect_date}.parquet"
    df = _align(df, ANALYSIS_SCHEMA)
    df = _normalize_collect_date(df)
    if path.exists():
        old = _align(pd.read_parquet(path), ANALYSIS_SCHEMA)
        old = _normalize_collect_date(old)
        df = pd.concat([old, df], ignore_index=True).drop_duplicates("news_id", keep="last")
    df = df.sort_values("publish_time", na_position="last").reset_index(drop=True)
    df.to_parquet(path, index=False, compression="zstd")
    logger.info("已写入 %s (%d 条)", path, len(df))
    return path


def save_watch_parquet(df, collect_date, watch_dir: str | Path) -> Path:
    """watch_target 当日整文件覆盖，zstd 落盘。"""
    watch_dir = Path(watch_dir)
    watch_dir.mkdir(parents=True, exist_ok=True)
    path = watch_dir / f"{collect_date}.parquet"
    df = _align(df, WATCH_SCHEMA)
    df.to_parquet(path, index=False, compression="zstd")
    logger.info("已写入 %s (%d 条)", path, len(df))
    return path


def save_analysis_mongo(db, df, collect_date) -> int:
    col = db["news_analysis"]
    ids = []
    docs = []
    for _, r in df.iterrows():
        doc = r.to_dict()
        if doc.get("publish_time") is None or pd.isna(doc.get("publish_time")):
            doc["publish_time"] = ""
        else:
            doc["publish_time"] = str(doc["publish_time"])
        doc["_id"] = f"{collect_date}|{doc['news_id']}"
        docs.append(doc)
        ids.append(doc["_id"])
    if docs:
        col.delete_many({"_id": {"$in": ids}})
        col.insert_many(docs)
    logger.info("news_analysis 今日整批重建 %d 条", len(docs))
    return len(docs)


def save_watch_mongo(db, docs, collect_date) -> int:
    """watch_target 整日 delete + insert。"""
    col = db["watch_target"]
    out = []
    for d in docs:
        doc = dict(d)
        doc["source_news_ids"] = list(doc.get("source_news_ids") or [])
        # 键口径要和 _merge_target 一致（无代码时用 NAME: 前缀）。这里曾写成 code or name，
        # 于是 (code="BK0474", name=…) 与 (code="", name="BK0474") 撞同一个 _id，
        # ordered insert 从第 541 条中止：一晚 1070 个标的只进了一半，日志仅一行 warning。
        key = f"{collect_date}|{doc['direction']}|{doc['code'] or ('NAME:' + doc['name'])}"
        doc["_id"] = md5(key.encode("utf-8")).hexdigest()[:16]
        out.append(doc)
    if out:
        col.delete_many({"collect_date": collect_date})
        col.insert_many(out)
    logger.info("watch_target 整日重建 %d 条", len(out))
    return len(out)


def create_indexes(db) -> None:
    db["news_analysis"].create_index([("collect_date", 1)])
    db["news_analysis"].create_index([("news_id", 1)])
    db["watch_target"].create_index([("collect_date", 1), ("direction", 1)])
    db["watch_target"].create_index([("code", 1)])
    db["watch_target"].create_index([("category", 1)])
    db["watch_target"].create_index([("collect_date", 1), ("code", 1)])
