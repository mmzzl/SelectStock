"""portfolio/revenue.py — 收益快照与 summary 聚合（R1-1/R1-4 插件计算）。"""
from __future__ import annotations


def _r2(v: float) -> float:
    return round(float(v), 2)


def snapshot(holding: dict, latest_price: float) -> dict:
    """单只持仓的收益快照（后端零算术，全部由插件产出）。"""
    quantity = int(holding["quantity"])
    cost = float(holding.get("cost_price", 0.0))
    latest = float(latest_price)
    pnl = (latest - cost) * quantity
    pnl_pct = ((latest - cost) / cost * 100) if cost > 0 else 0.0
    return {
        "code": str(holding["code"]),
        "name": str(holding.get("name", "")),
        "action": "",
        "action_reason": "",
        "latest_price": _r2(latest),
        "quantity": quantity,
        "holding_pnl": _r2(pnl),
        "pnl_pct": _r2(pnl_pct),
        "suggestion": "",
    }


def summarize(results: list[dict]) -> dict:
    counts: dict[str, int] = {}
    total_value = 0.0
    total_pnl = 0.0
    for r in results:
        counts[r["action"]] = counts.get(r["action"], 0) + 1
        total_value += float(r["latest_price"]) * int(r["quantity"])
        total_pnl += float(r.get("holding_pnl", 0.0))
    return {
        "action_counts": counts,
        "total_market_value": _r2(total_value),
        "total_pnl": _r2(total_pnl),
        "holding_count": len(results),
    }
