"""持仓诊断·插件客户端（R1-1 判定/收益 100% 收敛插件）。

M1（M2 未实现）默认 plugin_mode=mock：返回确定性 sell/hold/add 判定 + 收益快照，
仅供契约测试与本地自验。plugin_mode=http 时走 httpx 代理到插件地址（M2 接通后启用）。
"""

import logging

import httpx

from config import settings

logger = logging.getLogger("portfolio_plugin")

ACTIONS = ("sell", "hold", "add")


def _mock_analyze(holdings: list[dict]) -> dict:
    """模拟插件判定（sell/hold/add 轮换）+ 收益快照。仅测试占位，不接真实行情。"""
    results = []
    total_mv = 0.0
    total_pnl = 0.0
    for i, h in enumerate(holdings):
        action = ACTIONS[i % len(ACTIONS)]
        latest = round(h["cost_price"] * (1 + 0.005 * (i + 1)), 2)
        pnl = round((latest - h["cost_price"]) * h["quantity"], 2)
        results.append({
            "code": h["code"],
            "name": h.get("name", ""),
            "action": action,
            "action_reason": f"mock plugin: {action}",
            "latest_price": latest,
            "quantity": h["quantity"],
            "holding_pnl": pnl,
            "pnl_pct": round((latest / h["cost_price"]) * 100 - 100, 2),
            "suggestion": "",
        })
        total_mv += latest * h["quantity"]
        total_pnl += pnl
    summary = {
        "action_counts": {a: sum(1 for r in results if r["action"] == a) for a in ACTIONS},
        "total_market_value": round(total_mv, 2),
        "total_pnl": round(total_pnl, 2),
        "holding_count": len(results),
    }
    return {"results": results, "summary": summary}


async def _http_analyze(payload: dict) -> dict:
    url = settings.plugin_base_url.rstrip("/") + "/api/portfolio/analyze"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        body = resp.json()
    return {"results": body["results"], "summary": body.get("summary", {})}


# 插件契约只吃这几个字段；DB 行带 datetime 的 created_at/updated_at 会把 httpx json 序列化打炸
# （持仓诊断先踩过一次，利润分析把 trades 也带上，同一颗雷在两条路径上都会炸）。
def _canon_holdings(holdings: list[dict]) -> list[dict]:
    return [
        {
            "code": str(h["code"]),
            "name": str(h.get("name") or ""),
            "quantity": int(h["quantity"]),
            "cost_price": float(h["cost_price"]),
        }
        for h in holdings
    ]


def _canon_trades(trades: list[dict]) -> list[dict]:
    return [
        {
            "trade_date": str(t["trade_date"]),
            "stock_code": str(t["stock_code"]),
            "stock_name": str(t.get("stock_name") or ""),
            "direction": str(t.get("direction") or ""),
            "price": float(t.get("price") or 0.0),
            "quantity": int(t.get("quantity") or 0),
            "commission": float(t.get("commission") or 0.0),
            "stamp_tax": float(t.get("stamp_tax") or 0.0),
            "transfer_fee": float(t.get("transfer_fee") or 0.0),
            "settled_amount": float(t.get("settled_amount") or 0.0),
        }
        for t in trades
    ]


async def call_plugin_analyze(payload: dict) -> dict:
    """请求插件判定 + 收益快照；返回 {results, summary}。失败抛异常（R2-2 兜底由上层处理）。"""
    payload = {**payload, "holdings": _canon_holdings(payload.get("holdings", []))}
    if settings.plugin_mode == "http":
        return await _http_analyze(payload)
    logger.info("plugin_mode=mock，返回 mock 判定（holdings=%d）", len(payload.get("holdings", [])))
    return _mock_analyze(payload["holdings"])


def _mock_pnl(payload: dict) -> dict:
    """确定性 mock：成本为基准价，latest=成本*(1+0.5%)，已实现=0。仅测试占位。"""
    hold = {h["code"]: h for h in payload.get("holdings", [])}
    per = []
    for code, h in hold.items():
        cost = float(h.get("cost_price", 0.0))
        qty = int(h.get("quantity", 0))
        latest = round(cost * 1.005, 2)
        per.append({"code": code, "name": h.get("name", ""), "position": qty,
                    "avg_cost": round(cost, 4), "latest_price": latest,
                    "realized_pnl": 0.0,
                    "unrealized_pnl": round((latest - cost) * qty, 2),
                    "total_pnl": round((latest - cost) * qty, 2), "fees": 0.0})
    return {"per_code": per,
            "realized_summary": {"total_realized": 0.0, "total_fees": 0.0},
            "unrealized_summary": {"total_unrealized": round(sum(p["unrealized_pnl"] for p in per), 2),
                                   "total_value": round(sum(p["latest_price"] * p["position"] for p in per), 2),
                                   "holding_count": len(per)}}


async def _http_pnl(payload: dict) -> dict:
    url = settings.plugin_base_url.rstrip("/") + "/api/portfolio/pnl"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()


def _mock_quotes(holdings: list[dict]) -> dict:
    """mock 没有行情，就不给价：让持仓列表继续用诊断落库的快照。

    这里返回假价会把「诊断时算出来的盈亏」覆盖成编出来的数，比空着更糟——空着页面会显示
    快照并标注来源，假价则让人照着假价做决策。
    """
    return {"results": []}


async def _http_quotes(payload: dict) -> dict:
    url = settings.plugin_base_url.rstrip("/") + "/api/portfolio/quotes"
    # 短于 analyze 的 30 秒：这是每次打开持仓页都要走的一次问价，问不到就退回快照，不该拖住页面
    async with httpx.AsyncClient(timeout=8) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        body = resp.json()
    return {"results": body.get("results", [])}


async def call_plugin_quotes(payload: dict) -> dict:
    """请求插件的持仓现值（latest_price/盈亏/价格时刻与来源），不落 run、不做判定。"""
    payload = {**payload, "holdings": _canon_holdings(payload.get("holdings", []))}
    if settings.plugin_mode == "http":
        return await _http_quotes(payload)
    return _mock_quotes(payload["holdings"])


async def call_plugin_pnl(payload: dict) -> dict:
    """请求插件利润单点口径（per_code/realized_summary/unrealized_summary）。"""
    payload = {**payload,
               "trades": _canon_trades(payload.get("trades", [])),
               "holdings": _canon_holdings(payload.get("holdings", []))}
    if settings.plugin_mode == "http":
        return await _http_pnl(payload)
    return _mock_pnl(payload)