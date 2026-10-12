"""portfolio/analyzer.py — 插件 analyze 编排（R1-1/R1-4：判定+收益全部在此计算）。"""
from __future__ import annotations

import logging

from portfolio.judge import (HOLD_FLIP_PCT, HOLD_TAIL_P5_PCT, judge_holding,
                            sell_trigger_price)
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
        cost = float(h.get("cost_price", 0.0) or 0.0)
        base = snapshot(h, cost)
        row = provider.latest_row(h["code"])
        if row is None:
            base["action"] = "hold"
            base["action_reason"] = "行情缺失，维持原判不做交易建议"
            results.append(base)
            continue
        qty = int(h["quantity"])
        price = _price_for(h["code"], row, quotes)
        base["action"], base["action_reason"] = judge_holding(
            row, rules["sell"], rules["add"], cost_price=cost, price=price,
            disaster_stop_pct=rules["disaster_stop_pct"])
        # 价格线由同一条 sell 表达式反算（见 stock_selector/backtest/rule_trigger.py），
        # 和上面的判定同内核，不会出现「理由是跌破均线、给出的价却对不上」
        base["sell_trigger_price"] = sell_trigger_price(row, rules["sell"])
        base["latest_price"] = round(price, 2)
        base["holding_pnl"] = round((base["latest_price"] - cost) * qty, 2)
        base["pnl_pct"] = round(((base["latest_price"] - cost) / cost * 100), 2) if cost > 0 else 0.0
        base["suggestion"] = _conviction_line(base, cost, qty)
        results.append(base)
    return {"results": results, "summary": summarize(results)}


def _conviction_line(base: dict, cost: float, qty: int) -> str:
    """把「这条判定值多少钱」写成几个可核对的频率，不是一个分数。

    两个同类基线来自 2026-10-12 的全市场回验（见 judge 里 HOLD_* 的注释）：说的是「处在同样
    状态的持仓点整体后来怎么样了」，不是「这只票明天涨不涨」。它们只描述 hold 这一格，所以
    只有判成 hold 才印——判成 sell 还印「同类 hold 有 45% 转卖」是自己给自己抬杠。之所以只报
    尾部不报均值：实测过所有状态分箱的 10/20 日均值差都小于换手成本，只有 p5 单调有区分度。
    """
    price = float(base["latest_price"])
    if price <= 0:
        return ""
    bits: list[str] = []
    trigger = base.get("sell_trigger_price")
    if trigger:
        gap = (price - trigger) / price * 100
        bits.append(f"均线卖点在下方 {gap:.1f}%" if gap > 0 else f"已跌破均线卖点 {abs(gap):.1f}%")
    else:
        bits.append("本判据不含价格线")
    if base["action"] == "hold":
        bits.append(f"同类 hold 20 日内 {HOLD_FLIP_PCT:.0f}% 转为卖出")
        tail = price * qty * HOLD_TAIL_P5_PCT / 100
        bits.append(f"尾部 p5 −{HOLD_TAIL_P5_PCT:.1f}%≈再亏 {tail:,.0f} 元")
    if cost > 0 and price < cost:
        bits.append(f"回本需 {(cost - price) / price * 100:+.1f}%")
    return "｜".join(bits)


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
