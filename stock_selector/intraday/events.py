"""intraday/events.py — 基本面监控的盘中输入：当日 `watch_target`（公告/业绩/减持/龙虎榜）。

数据不是这里抓的：`scripts/analyze_news_llm.py` 已经在盘前/盘中把财经新闻过一遍 LLM，拆出
「哪只票、什么类别、利多还是利空、理由」写进 mongo 的 `watch_target`。盘中引擎只做一件事——
按代码取当天这批记录，转成带方向的信号，让风控去决定要不要推。

读不到（mongo 没起、当天还没跑分析）时返回空并留 WARNING：基本面腿缺了不该带走整条技术面
链路，盘中监控的底线是「行情在动、引擎在判」。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

COLLECTION = "watch_target"
_FIELDS = {"collect_date": 1, "code": 1, "name": 1, "category": 1, "direction": 1,
           "score": 1, "priority": 1, "reason": 1}


def _client():
    """连 config.yaml 里那台 mongo（和日线数据源同一份配置，不再另立一套）。"""
    from pymongo import MongoClient

    from ..datasource.mongo_source import _load_mongo_config

    mongo = _load_mongo_config()
    return MongoClient(host=mongo.get("host", "127.0.0.1"),
                       port=int(mongo.get("port", 27017)),
                       serverSelectionTimeoutMS=3000)


def load(day: str, codes: list[str]) -> dict[str, list[dict]]:
    """取当天这些代码的事件记录，按代码归组；没有事件返回空字典（不是 None）。"""
    if not codes:
        return {}
    client = None
    try:
        from ..datasource.mongo_source import _load_mongo_config

        db = _load_mongo_config().get("database", "eastmoney_news")
        client = _client()
        cursor = client[db][COLLECTION].find(
            {"collect_date": day, "code": {"$in": list(codes)}}, _FIELDS)
        out: dict[str, list[dict]] = {}
        for doc in cursor:
            code = str(doc.get("code") or "").zfill(6)
            if code:
                out.setdefault(code, []).append(doc)
        if out:
            logger.info("当日事件命中 %d 只（%s）", len(out), day)
        return out
    except Exception as exc:               # mongo 不可用/超时/集合缺失
        logger.warning("读 watch_target 失败（%s），本轮基本面腿为空: %s", day, exc)
        return {}
    finally:
        if client is not None:
            client.close()
