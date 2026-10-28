"""portfolio/analyzer.py — 插件 analyze 编排（R1-1/R1-4：判定+收益全部在此计算）。"""
from __future__ import annotations

import logging

from portfolio import conviction, hold_confidence, posterior
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
    # 形态卖出规则被显式关掉（active.json 给空列表 → 拼出来的 OR 串是空）：台账里
    # 「hold 的点 20 日内自行转卖 X%」这个状态就不存在了，那一行不能再印。
    form_sell_closed = not str(rules.get("sell") or "").strip()
    results: list[dict] = []
    for h in holdings:
        cost = float(h.get("cost_price", 0.0) or 0.0)
        base = snapshot(h, cost)
        row = provider.latest_row(h["code"])
        if row is None:
            base["action"] = "hold"
            base["action_reason"] = "行情缺失，维持原判不做交易建议"
            base.update(_chip_block(provider, h["code"]))
            results.append(base)
            continue
        qty = int(h["quantity"])
        price = _price_for(h["code"], row, quotes)
        base["action"], base["action_reason"] = judge_holding(
            row, rules["sell"], rules["add"], cost_price=cost, price=price,
            disaster_stop_pct=rules["disaster_stop_pct"],
            sell_rules=rules.get("sell_rules"), add_rules=rules.get("add_rules"))
        # 可信度（动作支持度，下行档口径；下行模型缺位回退上涨剧本）+ 博弈代理：
        # 模型缺失/核心特征不齐/ST 时自动回 None，不进 suggestion
        base.update(conviction.evaluate(row, action=base["action"]))
        # 价格线由同一条 sell 表达式反算（见 stock_selector/backtest/rule_trigger.py），
        # 和上面的判定同内核，不会出现「理由是跌破均线、给出的价却对不上」
        base["sell_trigger_price"] = sell_trigger_price(row, rules["sell"], rules.get("sell_rules"))
        base["latest_price"] = round(price, 2)
        base["holding_pnl"] = round((base["latest_price"] - cost) * qty, 2)
        base["pnl_pct"] = round(((base["latest_price"] - cost) / cost * 100), 2) if cost > 0 else 0.0
        base.update(hold_confidence.evaluate(base["action"], row, rules))
        base.update(_chip_block(provider, h["code"]))
        base["suggestion"] = _conviction_line(base, cost, qty,
                                              form_sell_closed=form_sell_closed)
        results.append(base)
    return {"results": results, "summary": summarize(results)}


def _chip_block(provider, code: str) -> dict:
    """三档价位（买入=近 60 日筹码主峰下沿、止盈=主峰上沿、止损=买入−2×ATR），口径随行返回。

    provider 不支持（老 FakeProvider、行情读取失败）时给 None + 一句原因：价位是展示块，
    不能因为它把整单诊断带崩，也不能留空让使用者读成「这只没有止损」。
    """
    fn = getattr(provider, "chip_levels", None)
    levels = None
    if fn is not None:
        try:
            levels = fn(code)
        except Exception:  # noqa: BLE001 - 展示层缺数不打断诊断
            logger.exception("筹码价位计算失败：%s", code)
            return {"buy_ref_price": None, "sell_target_price": None,
                    "buy_invalid_price": None,
                    "level_note": "筹码价位没算出来（行情读取失败）"}
    if not levels:
        return {"buy_ref_price": None, "sell_target_price": None, "buy_invalid_price": None,
                "level_note": "近 60 交易日历史不足（新上市或行情缺数），算不出筹码主峰"}
    from stock_selector.features import chips

    note = chips.levels_note(levels)
    return {"buy_ref_price": levels["buy_ref_price"],
            "sell_target_price": levels["sell_target_price"],
            "buy_invalid_price": levels["buy_invalid_price"],
            "level_note": note}


def _conviction_line(base: dict, cost: float, qty: int, *,
                     form_sell_closed: bool = False) -> str:
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
    # 方案 B 的那一行：hold 印「全部卖出规则都不触发」这个联合状态的事后分布，
    # sell 印**响的那条规则**自己的兑现率/卖飞率。它和下面的台账后验不是同一张表：
    # 台账是「判成这个状态的点」的整体分布，这里是「当前这套自定义规则集」回放出来的。
    conf = str(base.get("hold_confidence_text") or "").strip()
    if conf:
        bits.append(conf)
    if base["action"] == "hold":
        bits.extend(_posterior_bits(base["action"], price, qty, hide_flip=form_sell_closed))
    else:
        # 判成 sell 却只剩灾难线时，台账里的 sell 格是**旧规则**测出来的点集（和这条灾难线无关），
        # 拿它说事等于给一条没回放过的判据配事后数。台账重建后那一格干脆没有行。
        post = None if form_sell_closed else posterior.lookup(base["action"])
        if post:
            bits.append(_cross_hold_line(post))
    if cost > 0 and price < cost:
        bits.append(f"回本需 {(cost - price) / price * 100:+.1f}%")
    return "｜".join(bits)


def _cross_hold_line(post: dict) -> str:
    """sell/add 相对**同日 hold** 的实测差：只有同日配对才把当天的大盘水位抵消掉。

    这里原来印的是点级均值，而点级均值会被「判据扎堆的日子」主导：2026-10-13 实测同一份台账
    sell 格点级 +0.87%、日等权同日差分 −0.47pp，方向相反，等于把一条卖出规则夸成买入理由。
    台账没给差分（可比交易日不足）时退回上涨频率那一行，宁可口径旧一点也不印反掉的均值。
    """
    diff = post.get("vs_hold_pp")
    if diff is not None:
        t, days = post.get("vs_hold_t"), int(post.get("vs_hold_days") or 0)
        return (f"后 10 日比同日 hold {diff:+.2f}pp"
                + ("·三段同号" if post.get("vs_hold_era_stable") else "·三段不同号")
                + (f"(配对{days}日 t={t:+.1f})" if t is not None else f"(配对{days}日)"))
    return (f"同类判据后 10 日上涨 {post['up_pct']:.1f}%、"
            f"均值 {post['fwd10_mean_pct']:+.2f}%"
            + ("" if post["era_sign_stable"] else "（跨时代不同号）"))


def _posterior_bits(action: str, price: float, qty: int, *,
                    hide_flip: bool = False) -> list[str]:
    """hold 这一格的后验分布：上涨概率（含区间）、20 日自行转卖比例、尾部再亏多少元。

    数字直读 `data/judgement_ledger/posterior.parquet`（每天 19:00 全市场重算），台账没建时
    退回 judge.py 里 2026-10-12 那次全量标定的常数——两者口径相同，只是新鲜度不同。

    `hide_flip`：形态卖出规则关着时不看那张表的转卖列。它衡量的是「hold 点后来自己变成 sell」，
    而 sell 现在只剩成本灾难线——台账里那一列要么是没重算的旧规则数（46% 是占位规则测的），
    要么是重建后的 NaN，无论哪种都不是当前这套规则的数，印出来就是假数。
    """
    post = posterior.lookup(action)
    if post is None:
        post = {"up_pct": None, "n": 0, "credible": True, "era_sign_stable": True,
                "flip_sell_20_pct": HOLD_FLIP_PCT, "fwd20_p5_pct": -HOLD_TAIL_P5_PCT}
    tail_txt = ""
    if post.get("up_pct") is not None:
        tag = "" if post["credible"] else f"·样本不足(宽{post['ci_width_pp']:.1f}pp)"
        tail_txt = (f"同类 hold 后 10 日上涨 {post['up_pct']:.1f}%"
                    f"[{post['ci_low_pct']:.1f},{post['ci_high_pct']:.1f}]{tag}")
    flip = None if hide_flip else post.get("flip_sell_20_pct")
    p5 = post.get("fwd20_p5_pct")
    bits = []
    if tail_txt:
        bits.append(tail_txt)
    if flip is not None:
        bits.append(f"20 日内 {flip:.0f}% 自行转卖")
    if p5 is not None:
        bits.append(f"尾部 p5 {p5:.1f}%≈再亏 {price * qty * abs(p5) / 100:,.0f} 元")
    if not post["era_sign_stable"]:
        bits.append("三段历史不同号")
    return bits


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
