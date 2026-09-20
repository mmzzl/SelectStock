"""持仓簿记：交易记录 → 持仓（移动加权成本）。仅簿记（R1-2 例外）；
盈利归属插件，此处不产出任何盈亏字段。"""
from __future__ import annotations


def derive_holdings_from_trades(trades: list[dict]) -> tuple[list[dict], list[str]]:
    """按 stock_code 顺序处理交易记录，移动加权推导持仓快照。
    卖出的已实现盈亏属于插件口径，此处只维护数量与成本。"""
    errors: list[str] = []
    position: dict[str, dict] = {}

    for t in trades:
        code = t["stock_code"]
        qty = int(t["quantity"])
        price = float(t["price"])
        cur = position.setdefault(code, {"qty": 0, "cost": 0.0, "name": t.get("stock_name", "")})
        if t["direction"] == "buy":
            total = cur["qty"] + qty
            if total > 0:
                cur["cost"] = (cur["cost"] * cur["qty"] + price * qty) / total
            else:
                cur["cost"] = price
            cur["qty"] = total
        else:
            if qty > cur["qty"]:
                errors.append(f"卖出超过持仓: {code}（{cur['qty']}<{qty}）")
                cur["qty"] = 0  # 超卖作废该股持仓，不产出持仓行
                continue
            cur["qty"] -= qty  # 卖出不动成本基准

    holdings = [
        {"stock_code": code, "stock_name": v["name"], "quantity": v["qty"],
         "cost_price": round(v["cost"], 4)}
        for code, v in position.items() if v["qty"] > 0
    ]
    return holdings, errors