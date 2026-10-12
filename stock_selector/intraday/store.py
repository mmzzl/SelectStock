"""intraday/store.py — 决策落盘：一天一个 parquet（复盘用），可选同步进 mongo。

需求文档里这一格写的是 TDengine。这里换成 parquet：本机内存已经紧张（redis + mongo +
四个 python 服务），再引一个时序库只多不少，而复盘的读法是「按天全量扫 + 按代码过滤」，
parquet 的分区文件正好对上，和 `data/market_snapshot`、`data/stock_kline` 是同一套习惯。

`_id` 用 (日期, 代码, 信号种类, 分钟) 拼出来并先删后插：**重跑同一天不会产生重复行**，
这条性质比多写一个幂等检查便宜得多。
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DIR = ROOT / "data" / "intraday_decisions"
COLLECTION = "intraday_decision"

COLUMNS = ["day", "minute", "ts", "code", "name", "action", "kind", "signal_reason",
           "signal_price", "verdict", "blocked_by", "risk_reason", "shares", "order_amount",
           "fill_price", "slippage_bp", "pushed", "source"]

_FIELDS = COLUMNS + ["metrics"]


def row(signal, decision, *, ts: float, day: str, pushed: bool,
        metrics: dict | None = None) -> dict:
    """信号 + 风控决策 → 一行落盘记录。数值全部来自上游，这里只做拼装与取整。"""
    merged = {**(decision.metrics or {}), **(metrics or {}), **(signal.metrics or {})}
    return {
        "day": day,
        "minute": signal.minute,
        "ts": round(ts, 3),
        "code": signal.code,
        "name": signal.name,
        "action": signal.action,
        "kind": signal.kind,
        "signal_reason": signal.reason,
        "signal_price": signal.price,
        "verdict": decision.verdict,
        "blocked_by": decision.blocked_by,
        "risk_reason": decision.reason[:512],
        "shares": decision.shares,
        "order_amount": decision.order_amount,
        "fill_price": decision.est_fill_price,
        "slippage_bp": _round(decision.slippage_bp),
        # 1/0 而不是 True/False：决策要走 redis stream，_text 会写成 "True"，
        # 消费端再判真假就容易看错（告警进程按这一列决定发不发）。
        "pushed": 1 if pushed else 0,
        "source": merged.get("source", ""),
        "metrics": {k: v for k, v in merged.items() if isinstance(v, (int, float, str, bool))},
    }


def _round(value) -> float | None:
    return round(value, 2) if isinstance(value, (int, float)) else None


def append_parquet(rows: list[dict], day: str, out_dir: Path | None = None) -> Path | None:
    """按天落一个文件，同 (code, kind, minute) 后写覆盖先写。

    `metrics` 写之前转 JSON 字符串：各信号带的指标键不一样，pyarrow 会按第一行的键推
    struct schema，混合键直接抛错——存字符串最省，复盘时 `json.loads` 一下就行。
    """
    if not rows:
        return None
    out_dir = Path(out_dir) if out_dir else DEFAULT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{day}.parquet"
    frame = pd.DataFrame([{**item, "metrics": json.dumps(item.get("metrics") or {},
                                                        ensure_ascii=False)}
                          for item in rows], columns=_FIELDS)
    # 整轮都被风控拦下时这几列全是 None，pandas 把它们推成 object：跟已有文件的 float64
    # 列 concat 会报 FutureWarning，同一列的 dtype 还会随当天有没有放行而飘。空值是 NaN。
    for column in ("signal_price", "ts", "shares", "order_amount", "fill_price", "slippage_bp"):
        frame[column] = frame[column].astype(float)
    if path.is_file():
        try:
            frame = pd.concat([pd.read_parquet(path), frame], ignore_index=True)
        except Exception as exc:            # 文件损坏不能挡着新数据落盘
            logger.warning("%s 读取失败，本轮只写新数据: %s", path, exc)
    frame = frame.drop_duplicates(subset=["code", "kind", "minute"], keep="last")
    frame = frame.sort_values(["minute", "code"]).reset_index(drop=True)
    frame.to_parquet(path, index=False, compression="zstd")
    return path


def doc_id(item: dict) -> str:
    key = f"{item['day']}|{item['code']}|{item['kind']}|{item['minute']}"
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:16]


def append_mongo(rows: list[dict], db=None) -> int:
    """写 mongo（App/接口要按代码查历史决策时用）。传入已连接的 db 最省，缺则自建。"""
    if not rows:
        return 0
    owned = None
    try:
        if db is None:
            from pymongo import MongoClient

            from ..datasource.mongo_source import _load_mongo_config

            mongo = _load_mongo_config()
            owned = MongoClient(host=mongo.get("host", "127.0.0.1"),
                                port=int(mongo.get("port", 27017)),
                                serverSelectionTimeoutMS=3000)
            db = owned[mongo.get("database", "eastmoney_news")]
        ids = [doc_id(item) for item in rows]
        collection = db[COLLECTION]
        collection.delete_many({"_id": {"$in": ids}})
        docs = [{**item, "_id": doc_id(item)} for item in rows]
        collection.insert_many(docs)
        return len(docs)
    except Exception as exc:
        logger.warning("决策写 mongo 失败（parquet 已落盘，不影响复盘）: %s", exc)
        return 0
    finally:
        if owned is not None:
            owned.close()
