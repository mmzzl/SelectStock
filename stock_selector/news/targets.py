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


def _norm_name(name: str) -> str:
    """名称对不上大多是全角/空格问题：「张 裕Ａ」和「张裕A」是同一只。"""
    s = str(name or "").replace(" ", "").replace("\u3000", "").upper()
    return s.translate(str.maketrans("ＡＢＣＤ", "ABCD"))


def _names_match(left: str, right: str) -> bool:
    a, b = _norm_name(left), _norm_name(right)
    if not a or not b:
        return False
    return a == b or a in b or b in a


def _verify_code(code: str, name: str, name_maps) -> str:
    """代码要能被名称佐证，否则丢掉代码（名称仍保留）。

    LLM 会给出境外市场的代码：000830 在韩国是 SK海力士、在 A 股是鲁西化工，1347 是华虹宏力
    的港股代码。这些代码原样透出会让 App 把它当 A 股跳去 K 线页，显示的完全是另一只股票。
    BK 打头的是东财板块命名空间，本表没收录也不跟 A 股代码撞，直接留着。
    """
    if not code or not name_maps:
        return code
    board_map, stock_map = name_maps
    if code.upper().startswith("BK"):
        return code
    official = stock_map.get(code)
    if official is not None:
        return code if _names_match(name, official) else ""
    return ""


def _resolve_name(name: str, code: str, name_maps) -> str:
    """模型会把 hint 里的代码当名字抄过来（「BK0800」），能查到权威名就换掉。"""
    if not name_maps or not code:
        return name
    if name and name != code:
        return name
    board_map, stock_map = name_maps
    return board_map.get(code) or stock_map.get(code) or name


def _to_target(item, direction, hints, collect_date, llm_model, name_maps=None):
    name = item.get("name", "")
    code = _verify_code(_backfill_code(item, hints), name, name_maps)
    return {
        "collect_date": collect_date,
        "code": code,
        "name": _resolve_name(name, code, name_maps),
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


def build_watch_targets(rows, hints_by_news, collect_date, llm_model, name_maps=None):
    """把当日 news_analysis 行拆解合并为 watch_target 列表。

    hints_by_news: {news_id: [NormalizedHint, ...]} 用于 code 回填。
    name_maps: (board_map, stock_map)，用于校验代码确实是名称那只标的；不传则不校验。
    """
    merged = {}
    for row in rows:
        news_id = row.get("news_id", "")
        hints = hints_by_news.get(news_id, []) if hints_by_news else []
        for it in _dict_items(row, "bullish"):
            _merge_target(merged, _to_target(it, "bullish", hints, collect_date, llm_model,
                                            name_maps), news_id)
        for it in _dict_items(row, "bearish"):
            _merge_target(merged, _to_target(it, "bearish", hints, collect_date, llm_model,
                                            name_maps), news_id)
        for it in _dict_items(row, "watchlist"):
            _merge_target(merged, _to_target(it, "watch", hints, collect_date, llm_model,
                                            name_maps), news_id)
        for it in _dict_items(row, "risks"):
            _merge_target(merged, _risk_to_target(it, collect_date, llm_model), news_id)
    return list(merged.values())
