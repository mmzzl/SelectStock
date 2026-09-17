"""portfolio/analyzer.py — 插件 analyze 编排（R1-1/R1-4：判定+收益全部在此计算）。"""
from __future__ import annotations

from portfolio.judge import judge_holding
from portfolio.revenue import snapshot, summarize
from portfolio.rules import load_rules


def analyze_holdings(holdings: list[dict], provider) -> dict:
    """对每只持仓：取最新指标行 → RuleEvaluator 判定 → 收益快照 → summary。"""
    rules = load_rules()
    results: list[dict] = []
    for h in holdings:
        base = snapshot(h, 0.0 if h.get("cost_price") is None else float(h["cost_price"]))
        row = provider.latest_row(h["code"])
        if row is None:
            base["action"] = "hold"
            base["action_reason"] = "行情缺失，维持原判不做交易建议"
            results.append(base)
            continue
        base["action"], base["action_reason"] = judge_holding(row, rules["sell"], rules["add"])
        base["latest_price"] = round(float(row["close"]), 2)
        qty = int(h["quantity"])
        cost = float(h.get("cost_price", 0.0))
        base["holding_pnl"] = round((base["latest_price"] - cost) * qty, 2)
        base["pnl_pct"] = round(((base["latest_price"] - cost) / cost * 100), 2) if cost > 0 else 0.0
        results.append(base)
    return {"results": results, "summary": summarize(results)}
