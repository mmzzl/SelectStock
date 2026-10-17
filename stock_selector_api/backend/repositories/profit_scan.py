"""repositories/profit_scan.py — 逐笔盈亏扫描结果的只读查询。

数据全部由 `scripts/profit_scan.py` 一次性算完写进来（runs 一次跑一行，rows 一个「条件 × 卖法」一行），
这里只做筛选、排序、分页、按条件聚合 —— 后端零计算那条红线照样成立：一个百分点都不在这儿重算。

为什么聚合要在 Mongo 里做而不是前端：一次全量 62,208 行，按条件折叠成 972 行才看得过来，
把 6 万行发给浏览器只是为了折叠是不该付的账。
"""
import math
import re

from db.session import get_scan_collection

RUNS = "profit_scan_runs"
ROWS = "profit_scan_rows"

# 排序白名单：不在表里的键一律不走 $sort，拼错或塞进来的东西到这儿就挡掉
RULE_ORDER = {"max_net_pp": "max_net_pp", "best_t_day": "best_t_day", "max_trades": "max_trades",
              "n_pass": "n_pass", "cond_id": "cond_id"}
ROW_ORDER = {"net_per_trade_pp", "excess_pp", "t_day", "t_trade", "win_rate", "payoff",
             "n_trades", "trades_per_stock_year", "profile"}

_LIST_FIELDS = {"_id": 0, "run_id": 1, "created_at": 1, "window": 1, "n_days": 1,
                "date_from": 1, "date_to": 1, "n_conditions": 1, "n_profiles": 1, "n_trials": 1,
                "t_required": 1, "cost_pp": 1, "trade_amount": 1, "step": 1, "files": 1,
                "stocks": 1, "n_pass": 1, "elapsed_s": 1, "fills": 1, "holdout_start": 1}
_ROW_FIELDS = {"_id": 0, "run_id": 1, "cond_id": 1, "cond_label": 1, "kind": 1, "field": 1,
               "expr": 1, "profile": 1, "hold_days": 1, "take_profit": 1, "stop_loss": 1,
               "n_cells": 1, "n_trades": 1, "n_no_fill": 1, "days": 1, "gross_pp": 1,
               "net_per_trade_pp": 1, "day_mean_pp": 1, "win_rate": 1, "payoff": 1, "sd_pp": 1,
               "t_day": 1, "t_trade": 1, "era_net_pp": 1, "trades_per_stock_year": 1,
               "bench_net_pp": 1, "excess_pp": 1, "verdict": 1, "failed": 1, "checks": 1,
               "note": 1}


def _finite(doc: dict) -> dict:
    """盈亏比可以是 Infinity（一笔都没亏过），而 Infinity 序列化出来的 JSON 前端 parse 会炸。

    NaN 不动：它是 Mongo 里的合法值，页面按「没数」显示 Infinity 才必须换成 null。
    """
    for k, v in doc.items():
        if isinstance(v, float) and math.isinf(v):
            doc[k] = None
    for v in doc.values():
        if isinstance(v, dict):
            _finite(v)
        elif isinstance(v, list):
            for i, item in enumerate(v):
                if isinstance(item, dict):
                    _finite(item)
    return doc


def _row_match(run_id: str, kind: str = "", q: str = "", verdict: str = "",
               min_trades: int = 0) -> dict:
    m: dict = {"run_id": run_id}
    if kind:
        m["kind"] = kind
    if verdict:
        m["verdict"] = verdict
    if min_trades > 0:
        # 「折叠只看笔数够的行」这条筛法要显式说：64 种卖法里 3 笔就 +15pp 的那种组合
        # 不该出现在「最好的卖法」里（用户定的门槛之一就是 ≥300 笔）
        m["n_trades"] = {"$gte": min_trades}
    if q:
        pat = re.compile(re.escape(q)[:40], re.I)
        m["$or"] = [{"cond_label": pat}, {"expr": pat}, {"cond_id": pat}]
    return m


async def latest_run(run_id: str = "") -> dict | None:
    col = get_scan_collection(RUNS)
    if run_id:
        doc = await col.find_one({"run_id": run_id}, _LIST_FIELDS)
    else:
        doc = await col.find({}, _LIST_FIELDS).sort("created_at", -1).limit(1).to_list(1)
        doc = doc[0] if doc else None
    return _finite(doc) if doc else None


async def list_runs(limit: int = 20) -> list[dict]:
    docs = await get_scan_collection(RUNS).find({}, _LIST_FIELDS) \
        .sort("created_at", -1).limit(limit).to_list(limit)
    return [_finite(d) for d in docs]


async def run_stats(run_id: str) -> dict:
    """判定分布 + 各条门槛各卡掉多少：页面顶部那行「过 0 / 不过 640」就靠它。"""
    kinds_pass = {"$sum": {"$cond": [{"$eq": ["$verdict", "过"]}, 1, 0]}}
    pipe = [
        {"$match": {"run_id": run_id}},
        {"$facet": {
            "verdict": [{"$group": {"_id": "$verdict", "n": {"$sum": 1}}}],
            "failed": [{"$unwind": "$failed"}, {"$group": {"_id": "$failed", "n": {"$sum": 1}}}],
            "kinds": [{"$group": {"_id": "$kind", "n": {"$sum": 1}, "pass": kinds_pass}}],
        }},
    ]
    out = await get_scan_collection(ROWS).aggregate(pipe, allowDiskUse=True).to_list(1)
    f = out[0] if out else {"verdict": [], "failed": [], "kinds": []}
    return {
        "verdict": {d["_id"] or "未判": d["n"] for d in f["verdict"]},
        "failed": {d["_id"]: d["n"] for d in f["failed"]},
        "kinds": sorted(({"kind": d["_id"], "rows": d["n"], "pass": d["pass"]} for d in f["kinds"]),
                        key=lambda x: -x["rows"]),
    }


async def list_rules(run_id: str, *, kind: str = "", q: str = "", verdict: str = "",
                     min_trades: int = 0, page: int = 1, size: int = 20,
                     order: str = "max_net_pp") -> dict:
    """层一：一条买入条件一行，把它那 64 种卖法折起来（最好卖法看扣后每笔）。"""
    key = order if order in RULE_ORDER else "max_net_pp"
    pipe = [
        {"$match": _row_match(run_id, kind, q, verdict, min_trades)},
        # 先按扣后每笔排好，$last 才能稳定取到「最好的那个卖法」那一行的各列
        {"$sort": {"net_per_trade_pp": 1}},
        {"$group": {
            "_id": "$cond_id",
            "cond_label": {"$first": "$cond_label"}, "kind": {"$first": "$kind"},
            "field": {"$first": "$field"}, "expr": {"$first": "$expr"},
            "n_profiles": {"$sum": 1},
            "n_pass": {"$sum": {"$cond": [{"$eq": ["$verdict", "过"]}, 1, 0]}},
            "max_net_pp": {"$max": "$net_per_trade_pp"},
            "max_trades": {"$max": "$n_trades"},
            "best_t_day": {"$max": "$t_day"},
            "best_profile": {"$last": "$profile"},
            "best_hold": {"$last": "$hold_days"},
            "best_win_rate": {"$last": "$win_rate"},
            "best_payoff": {"$last": "$payoff"},
            "best_excess_pp": {"$last": "$excess_pp"},
            "best_net_trades": {"$last": "$n_trades"},
            "best_per_stock_year": {"$last": "$trades_per_stock_year"},
            "best_failed": {"$last": "$failed"},
        }},
        {"$addFields": {"cond_id": "$_id"}},
        {"$project": {"_id": 0}},
        {"$facet": {
            "data": [{"$sort": {key: -1 if key != "cond_id" else 1, "cond_id": 1}},
                     {"$skip": (page - 1) * size}, {"$limit": size}],
            "meta": [{"$count": "total"}],
        }},
    ]
    out = await get_scan_collection(ROWS).aggregate(pipe, allowDiskUse=True).to_list(1)
    f = out[0] if out else {"data": [], "meta": []}
    total = f["meta"][0]["total"] if f["meta"] else 0
    return {"items": [_finite(d) for d in f["data"]], "total": total}


async def list_profiles(run_id: str, cond_id: str, order: str = "net_per_trade_pp") -> list[dict]:
    """层二：这条条件在 64 种卖法下分别赚多少。"""
    col = get_scan_collection(ROWS)
    flt = {"run_id": run_id, "cond_id": cond_id}
    asc = order in ("profile",)
    docs = await col.find(flt, _ROW_FIELDS).sort(order if order in ROW_ORDER else "net_per_trade_pp",
                                                 1 if asc else -1).limit(200).to_list(200)
    return [_finite(d) for d in docs]


async def get_row(run_id: str, cond_id: str, profile: str) -> dict | None:
    """层三：单行明细，含 8 项归因（why）与相邻档读数（neighbour_bands_pp）。"""
    doc = await get_scan_collection(ROWS).find_one(
        {"run_id": run_id, "cond_id": cond_id, "profile": profile},
        {**_ROW_FIELDS, "_id": 0, "neighbour_bands_pp": 1, "why": 1})
    return _finite(doc) if doc else None


async def profile_options(run_id: str) -> list[str]:
    """筛选下拉要的那 64 个卖法名（按卖法字典序，别按数值乱序）。"""
    return sorted(await get_scan_collection(ROWS).distinct("profile", {"run_id": run_id}))


async def kind_options(run_id: str) -> list[str]:
    return sorted(await get_scan_collection(ROWS).distinct("kind", {"run_id": run_id}))
