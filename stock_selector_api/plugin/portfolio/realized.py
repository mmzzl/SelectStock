"""利润插件核心：移动加权成本序列（单点口径）+ 已实现/浮动盈亏。
后端持仓簿记与本模块用同一移动加权算法，回归测试锁一致（R1-2）。"""
from __future__ import annotations


def _r(v: float) -> float:
    return round(float(v), 2)


def _cost_step(qty: int, cost: float, buy_qty: int, price: float) -> float:
    total = qty + buy_qty
    return (cost * qty + price * buy_qty) / total if total > 0 else price


def trade_order_key(t: dict) -> tuple:
    """簿记排序键：同一天内买入先于卖出。交割单只精确到日，同日买卖的真实先后无从得知；
    卖在前会在无仓可卖时被截断，最后凭空留下持仓，买在前不会。
    后端持仓簿记（services/derive_holdings.py）用同一个键，回归测试锁一致（R1-2）。"""
    return (t["stock_code"], t["trade_date"], 0 if t["direction"] == "buy" else 1)


def compute_pnl(trades: list[dict], holdings: list[dict], latest_price_fn) -> dict:
    """trades 逐条移动加权；卖出即时结算已实现盈亏（费用扣减）。
    latest_price_fn(code)->float 现价；持仓快照仅兜底现成数量/成本。"""
    pos: dict[str, dict] = {}
    realized: dict[str, float] = {}
    fees: dict[str, float] = {}

    for t in sorted(trades, key=trade_order_key):
        code = t["stock_code"]
        cur = pos.setdefault(code, {"name": t.get("stock_name", ""), "qty": 0, "cost": 0.0})
        fee = float(t.get("commission", 0.0)) + float(t.get("stamp_tax", 0.0)) + float(t.get("transfer_fee", 0.0))
        fees[code] = fees.get(code, 0.0) + fee
        qty = int(t["quantity"])
        price = float(t["price"])
        if t["direction"] == "buy":
            cur["cost"] = _cost_step(cur["qty"], cur["cost"], qty, price)
            cur["qty"] += qty
        else:
            if qty > cur["qty"]:   # 容错：超卖只按现持仓结算
                qty = cur["qty"]
            realized[code] = realized.get(code, 0.0) + (price - cur["cost"]) * qty - fee
            cur["qty"] -= qty

    # holdings 提供无交易记录的仓位兜底（名称/成本）
    for h in holdings:
        code = h["code"]
        cur = pos.setdefault(code, {"name": h.get("name", ""), "qty": int(h.get("quantity", 0)),
                                    "cost": float(h.get("cost_price", 0.0))})
        if cur["qty"] == 0:
            cur["qty"] = int(h.get("quantity", 0))
            cur["cost"] = float(h.get("cost_price", 0.0))
        cur["name"] = h.get("name", cur.get("name", ""))

    per_code = []
    total_value = 0.0
    total_unrealized = 0.0
    total_realized = 0.0
    total_fees = 0.0
    for code, cur in pos.items():
        # 只给在持仓位取价：已平仓的代码行情对总额没有任何贡献（数量 0），却要把每次外部请求
        # 都赌上——交割单里三十几个代码全打 AKShare，任一失败整页利润分析就 503。
        if cur["qty"] > 0:
            latest = float(latest_price_fn(code))
        else:
            latest = float(cur["cost"])
        unreal = (latest - cur["cost"]) * cur["qty"] if cur["qty"] > 0 else 0.0
        total_value += latest * cur["qty"]
        total_unrealized += unreal
        rp = realized.get(code, 0.0)
        total_realized += rp
        total_fees += fees.get(code, 0.0)
        per_code.append({
            "code": code, "name": cur["name"],
            "position": cur["qty"], "avg_cost": _r(cur["cost"]),
            "latest_price": _r(latest),
            "realized_pnl": _r(rp), "unrealized_pnl": _r(unreal),
            "total_pnl": _r(rp + unreal), "fees": _r(fees.get(code, 0.0)),
        })
    return {
        "per_code": per_code,
        "realized_summary": {"total_realized": _r(total_realized),
                             "total_fees": _r(total_fees)},
        "unrealized_summary": {"total_unrealized": _r(total_unrealized),
                               "total_value": _r(total_value),
                               # per_code 是「所有交易过的代码」，持仓数只算还在场的，否则利润分析页会写「持仓 76 只」
                               "holding_count": sum(1 for r in per_code if r["position"] > 0)},
    }
