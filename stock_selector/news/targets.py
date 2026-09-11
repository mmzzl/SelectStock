"""从 news_analysis 行拆解并当日合并 watch_target。"""

from __future__ import annotations

import logging

logger = logging.getLogger("news.targets")


def _dict_items(row, key):
    """仅返回 dict 元素；非 dict 跳过并 WARNING。"""
    val = row.get(key, []) or []
    non_dict = [x for x in val if not isinstance(x, dict)]
    if non_dict:
        logger.warning("[watch_target] %s 含 %d 个非 dict 元素，已跳过", key, len(non_dict))
    return [x for x in val if isinstance(x, dict)]


def _backfill_code(item, hints):
    """用候选代码表回填空 code；无法确定保持空串（不猜）。"""
    if item.get("code"):
        return item["code"]
    name = item.get("name")
    if not name:
        return ""
    for h in hints or []:
        if h.get("name") == name and h.get("code"):
            return h["code"]
    pool = [h for h in (hints or []) if h.get("code")]
    if len(pool) == 1:
        return pool[0]["code"]
    return ""


def _to_target(item, direction, hints, collect_date, llm_model):
    return {
        "collect_date": collect_date,
        "code": _backfill_code(item, hints),
        "name": item.get("name", ""),
        "category": item.get("category", ""),
        "direction": direction,
        "score": int(item.get("score", 0) or 0),
        "priority": item.get("priority", "") if direction == "watch" else "",
        "reason": item.get("reason", ""),
        "source_news_ids": [],
        "llm_model": llm_model,
    }


def _risk_to_target(item, collect_date, llm_model):
    desc = item.get("desc", "") or ""
    return {
        "collect_date": collect_date,
        "code": "",
        "name": desc[:20],
        "category": "风险",
        "direction": "risk",
        "score": int(item.get("score", 0) or 0),
        "priority": "",
        "reason": desc,
        "source_news_ids": [],
        "llm_model": llm_model,
    }


def _merge_target(merged, target, news_id):
    key = f"{target['direction']}|{target['code'] or ('NAME:' + target['name'])}"
    old = merged.get(key)
    if old is None:
        if news_id:
            target["source_news_ids"] = [news_id]
        merged[key] = target
        return
    if news_id and news_id not in old["source_news_ids"]:
        old["source_news_ids"].append(news_id)
    for field in ("name", "priority", "reason", "code"):
        if not old.get(field) and target.get(field):
            old[field] = target[field]


def build_watch_targets(rows, hints_by_news, collect_date, llm_model):
    """把当日 news_analysis 行拆解合并为 watch_target 列表。

    hints_by_news: {news_id: [NormalizedHint, ...]} 用于 code 回填。
    """
    merged = {}
    for row in rows:
        news_id = row.get("news_id", "")
        hints = hints_by_news.get(news_id, []) if hints_by_news else []
        for it in _dict_items(row, "bullish"):
            _merge_target(merged, _to_target(it, "bullish", hints, collect_date, llm_model), news_id)
        for it in _dict_items(row, "bearish"):
            _merge_target(merged, _to_target(it, "bearish", hints, collect_date, llm_model), news_id)
        for it in _dict_items(row, "watchlist"):
            _merge_target(merged, _to_target(it, "watch", hints, collect_date, llm_model), news_id)
        for it in _dict_items(row, "risks"):
            _merge_target(merged, _risk_to_target(it, collect_date, llm_model), news_id)
    return list(merged.values())
