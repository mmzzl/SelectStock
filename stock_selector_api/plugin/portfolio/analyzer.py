"""portfolio/analyzer.py — 插件 analyze 编排（R1-1/R1-4：判定+收益全部在此计算）。"""
from __future__ import annotations

import logging

from portfolio.judge import judge_holding, sell_trigger_price
from portfolio.revenue import snapshot, summarize
from portfolio.rules import load_rules

logger = logging.getLogger(__name__)


def analyze_holdings(holdings: list[dict], provider, quotes: dict | None = None) -> dict:
    """对每只持仓：取最新指标行 → RuleEvaluator 判定 → 收益快照 → summary。

    `quotes`（`pricing.resolve` 的结果）只换**价格**，不换判定：指标行仍然来自日线 parquet，
    而 `latest_price`/`holding_pnl`/`pnl_pct` 用实时价——18:00 之前手动触发诊断时，parquet
    最后一根还是上一交易日的收盘，落库的盈亏不能按那个数算。
    """
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
        # 价格线由同一条 sell 表达式反算（见 stock_selector/backtest/rule_trigger.py），
        # 和上面的判定同内核，不会出现「理由是跌破均线、给出的价却对不上」
        base["sell_trigger_price"] = sell_trigger_price(row, rules["sell"])
        price = _price_for(h["code"], row, quotes)
        base["latest_price"] = round(price, 2)
        qty = int(h["quantity"])
        cost = float(h.get("cost_price", 0.0))
        base["holding_pnl"] = round((base["latest_price"] - cost) * qty, 2)
        base["pnl_pct"] = round(((base["latest_price"] - cost) / cost * 100), 2) if cost > 0 else 0.0
        results.append(base)
    return {"results": results, "summary": summarize(results)}


def _price_for(code: str, row: dict, quotes: dict | None) -> float:
    quote = (quotes or {}).get(str(code))
    return float(quote["price"]) if quote else float(row["close"])


def quote_holdings(holdings: list[dict], natural_day: str) -> dict:
    """持仓页要的「现在这一眼」的价：不跑指标、不做判定，只给价格、价格和盈亏的**时刻与来源**。

    和 analyze 分开是因为频率不同：持仓列表每次打开都要刷新，诊断一天一次并落库。收益算术仍然
    在这里（R1-1），后端拿到的是可以直接排版的数。行情问不到时 `price_source` 退成 `kline`，
    `price_time` 是那根日线的日期——页面据此写明「收盘快照」而不是假装它是最新价。
    """
    from portfolio.pricing import PricingError, resolve

    codes = [str(h["code"]) for h in holdings]
    try:
        quotes = resolve(codes, natural_day)
    except PricingError:
        # 一只票取不到价不能让整页没有价（退市/新标的没建过文件时 AKShare 也会抛）
        quotes = {}
        for code in codes:
            try:
                quotes[code] = resolve([code], natural_day)[code]
            except PricingError:
                logger.warning("实时报价缺失 %s：%s 这天取不到价", code, natural_day)
    out = []
    for h in holdings:
        code = str(h["code"])
        q = quotes.get(code) or {}
        cost = float(h.get("cost_price", 0.0) or 0.0)
        qty = int(h.get("quantity", 0) or 0)
        price = q.get("price")
        row = {"code": code, "name": h.get("name", ""), "quantity": qty,
               "latest_price": round(price, 2) if price else None,
               "price_time": q.get("price_time", ""), "price_source": q.get("price_source", "")}
        if price:
            row["holding_pnl"] = round((price - cost) * qty, 2)
            row["pnl_pct"] = round((price - cost) / cost * 100, 2) if cost > 0 else 0.0
            row["market_value"] = round(price * qty, 2)
        out.append(row)
    return {"results": out}
