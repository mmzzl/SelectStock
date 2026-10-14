"""risk/rules.py — 九条风控规则：每条只回答「这个方向这一单能不能成交、值不值得成交」。

约定（引擎依赖它，别在规则里各写各的）：
- 返回 `Verdict`，`status` 取 allow / block / adjust；**block 必须给出人能看懂的中文理由**，
  告警链路要把「为什么没推」原样说给用户；
- 数据不可得时**放行并留 metric 说明**，不猜数也不误杀——和买入风险分同一个失败姿态；
  唯一例外是「连成交价都没有」，那时下单量无从换算，只能 block；
- 规则不改上下文以外的状态，允许读写引擎传入的 `state`（仓位计划、滑点估计在这条流水线上接力）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .context import RiskContext


@dataclass
class Verdict:
    name: str
    status: str = "allow"          # allow | block | adjust
    reason: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return self.status == "block"


def _wan(value: float | None) -> str:
    return f"{(value or 0) / 1e4:.0f}万"


def tradability(ctx: RiskContext, cfg, state: dict) -> Verdict:
    """涨跌停可成交性：涨停买不进、跌停卖不出，策略要识别。

    成交价与涨跌停价取交易所原值（见 risk/snapshot.py），不按板块百分比自己推——ST、
    北交所、基金各有各的规则，硬推一定算错。没有快照的历史日期退回日线涨停列 `lu`；
    日线只有涨停没有跌停，所以卖出侧无口径时放行并在指标里写明，而不是猜一个跌停价。
    """
    price = ctx.price
    if ctx.is_fund:
        return Verdict("tradability", metrics={"skipped": "fund"})
    if cfg.use_limit and price:
        if ctx.action == "buy" and ctx.quote.get("at_limit_up"):
            return Verdict("tradability", "block",
                           f"已涨停（{price} ≥ 涨停价 {ctx.quote.get('limit_up')}），买单排队买不进",
                           {"limit_up": ctx.quote.get("limit_up")})
        if ctx.action == "sell" and ctx.quote.get("at_limit_down"):
            return Verdict("tradability", "block",
                           f"已跌停（{price} ≤ 跌停价 {ctx.quote.get('limit_down')}），卖单排队卖不出",
                           {"limit_down": ctx.quote.get("limit_down")})
        if ctx.quote:
            return Verdict("tradability", metrics={
                "limit_up": ctx.quote.get("limit_up"),
                "limit_down": ctx.quote.get("limit_down"),
                "source": ctx.quote.get("source", ""),
            })
    last = _last_bar(ctx.kline)
    if last is None:
        return Verdict("tradability", metrics={"limit_data": False})
    if ctx.action == "buy" and bool(last.get("lu")):
        return Verdict("tradability", "block", "基准日封涨停，按次日开盘价买不进",
                       {"limit_data": "kline_lu"})
    return Verdict("tradability", metrics={"limit_data": "kline_lu_only",
                                           "limit_down_unknown": ctx.action == "sell"})


def t_plus_1(ctx: RiskContext, cfg, state: dict) -> Verdict:
    """T+1：今天买入的股票，最快明天才卖得掉。

    持仓成本市值只说明「拿着」，说明不了「什么时候拿到的」，所以买入日由调用方从交割成交
    （`trade_records` 的 `trade_date`）里查好塞进上下文。交割单当天没导就判不了——**判不了
    就放行并留指标**，静默不推比推早一条更难查。
    """
    if not cfg.t_plus_1:
        return Verdict("t_plus_1", metrics={"skipped": "disabled"})
    if ctx.action != "sell":
        return Verdict("t_plus_1", metrics={"skipped": "buy_side"})
    if ctx.bought_today is None:
        return Verdict("t_plus_1", metrics={"skipped": "no_trade_date"})
    if ctx.bought_today:
        return Verdict("t_plus_1", "block",
                       "T+1：该股今日有买入成交，明天才是可卖日", {"bought_today": True})
    return Verdict("t_plus_1", metrics={"bought_today": False})


def buy_quota(ctx: RiskContext, cfg, state: dict) -> Verdict:
    """买入名额：资金有限，一天只盯名单里排最前的那几只。

    名次来自调用方给的盯盘名单顺序（盘中=选股批次按 App 展示序，即预期超额降序），
    所以这条不是「又加了一个筛选因子」，而是把「买得下几只」这件事说清楚。持仓加仓、
    手工指定代码没有名次（`pool_rank=None`）→ 放行。
    """
    if not cfg.max_buy_codes:
        return Verdict("buy_quota", metrics={"skipped": "no_limit"})
    if ctx.action != "buy":
        return Verdict("buy_quota", metrics={"skipped": "sell_side"})
    if ctx.pool_rank is None:
        return Verdict("buy_quota", metrics={"skipped": "no_rank"})
    if ctx.pool_rank >= cfg.max_buy_codes:
        return Verdict("buy_quota", "block",
                       f"买入只看当日名单前 {cfg.max_buy_codes} 只，本票排第 {ctx.pool_rank + 1}；"
                       "资金就这些，不铺开",
                       {"pool_rank": ctx.pool_rank, "max_buy_codes": cfg.max_buy_codes})
    return Verdict("buy_quota", metrics={"pool_rank": ctx.pool_rank})


def liquidity(ctx: RiskContext, cfg, state: dict) -> Verdict:
    """流动性：日均成交额太低的票直接过滤——成交稀，滑点和冲击成本会把信号吃掉。"""
    if not cfg.min_avg_amount:
        return Verdict("liquidity", metrics={"skipped": "disabled"})
    avg = ctx.avg_amount
    if avg is None:
        return Verdict("liquidity", metrics={"avg_amount": None})
    metrics = {"avg_amount": round(avg, 2)}
    if avg < cfg.min_avg_amount:
        return Verdict("liquidity", "block",
                       f"近 20 日日均成交额 {_wan(avg)}，低于流动性下限 {_wan(cfg.min_avg_amount)}",
                       metrics)
    return Verdict("liquidity", metrics=metrics)


def position(ctx: RiskContext, cfg, state: dict) -> Verdict:
    """单票仓位上限：总资金 × 上限比例，扣掉已持有市值后折成整手股数。

    买入侧受额度约束；卖出侧能卖多少只看手里有多少（`enforce_cap=False`）。没配总资金
    （capital=0）时不折算建议股数——宁可不给数量，也不编一个「20 万」出来误导用户。
    """
    from . import sizing

    if ctx.action != "buy" and not ctx.shares and not ctx.position_value:
        return Verdict("position", metrics={"skipped": "sell_without_quantity"})
    if not cfg.capital or not cfg.max_position_pct:
        if ctx.action != "buy" or not ctx.price:
            return Verdict("position", metrics={"skipped": "no_capital"})
        return Verdict("position", "block", "未配置总资金（risk.capital），无法给出下单股数", {})
    plan = sizing.plan_position(ctx.price, cfg.capital, cfg.max_position_pct,
                                ctx.position_value, ctx.shares,
                                enforce_cap=ctx.action == "buy")
    state["plan"] = plan
    metrics = {"shares": plan.shares, "order_amount": round(plan.amount, 2),
               "cap_value": round(plan.cap_value, 2) if plan.cap_value != float("inf") else None,
               "held_value": round(plan.used_value, 2)}
    if plan.shares is None:
        return Verdict("position", "block", plan.reason or "缺少成交价，无法折算下单股数", metrics)
    if plan.shares <= 0:
        return Verdict("position", "block",
                       plan.reason or f"单票额度 {plan.cap_value:.0f} 元已用满，买不进任何整手",
                       metrics)
    if plan.over_cap:
        return Verdict("position", "adjust", plan.reason, metrics)
    return Verdict("position", metrics=metrics)


def cash_budget(ctx: RiskContext, cfg, state: dict) -> Verdict:
    """当日可用资金：买入金额不能超出现在还剩的钱，超了就按剩下的钱折算股数（adjust）。

    和 `position` 的分工：那条管**单票**上限（这只票最多拿多少），这条管**账户**总钱
    （今天一共还出得出多少钱）。单票 20% 的额度如果不管别的持仓，五只票一起放行就等于
    买了 100% 还多——盘中一次推 30 只买入时就是这个结果。
    现金口径由调用方给（盘中 = 总资金 − 现有持仓成本 + 当日卖出回款）；没给就放行留指标。
    """
    from . import sizing

    if not cfg.cash_check:
        return Verdict("cash_budget", metrics={"skipped": "disabled"})
    if ctx.action != "buy":
        return Verdict("cash_budget", metrics={"skipped": "sell_side"})
    if ctx.cash_left is None:
        return Verdict("cash_budget", metrics={"skipped": "no_cash"})
    plan = state.get("plan")
    amount = float(plan.amount) if plan else 0.0
    cash = max(0.0, float(ctx.cash_left))
    metrics = {"cash_left": round(cash, 2), "order_amount": round(amount, 2)}
    if amount <= cash:
        return Verdict("cash_budget", metrics=metrics)
    if not ctx.price:
        return Verdict("cash_budget", "block",
                       f"可用资金只剩 {_wan(cash)}，且没有成交价可折算股数", metrics)
    shrunk = sizing.plan_position(ctx.price, cash, 1.0, 0.0, None)
    # 改写计划而不是只报个数：后面的滑点规则与引擎取的都是 state["plan"]，
    # 不改就变成「拦了但下单量还是原来那个」。
    state["plan"] = shrunk
    if not shrunk.shares:
        return Verdict("cash_budget", "block",
                       f"可用资金 {_wan(cash)} 不足一手（现价 {ctx.price}）", metrics)
    return Verdict("cash_budget", "adjust",
                   f"建议 {plan.shares} 股要 {_wan(amount)}，超出可用资金 {_wan(cash)}，"
                   f"按 {shrunk.shares} 股下单", metrics)


def slippage(ctx: RiskContext, cfg, state: dict) -> Verdict:
    """滑点预估：行情推送有延迟，看到的价格不一定成交得到，按估计值复核一遍再放行。

    价格基准用**行情最新价**而不是信号价：滑点要从「现在这个价」起算，信号价与它的差
    正是漂移项，两者不能混用同一个数。
    """
    from . import sizing

    market = ctx.quote.get("price") or ctx.price
    if not market:
        return Verdict("slippage", metrics={"skipped": "no_price"})
    plan = state.get("plan")
    # 没有仓位计划（未配总资金/卖出侧没给数量）时，用一个探测单额估冲击，滑点判定照样成立
    amount = plan.amount if plan else cfg.probe_order_amount
    slip = sizing.estimate(market, amount, quote=ctx.quote, avg_amount=ctx.avg_amount,
                           daily_vol_pct=ctx.daily_vol_pct, signal_price=ctx.signal_price,
                           action=ctx.action, min_spread_bp=cfg.min_spread_bp)
    state["slippage"] = slip
    metrics = slip.metrics
    if ctx.signal_price and ctx.quote.get("price") and slip.drift_bp > cfg.max_drift_bp:
        return Verdict("slippage", "block",
                       f"信号价 {ctx.signal_price} 与最新价 {ctx.quote.get('price')} 偏离 "
                       f"{slip.drift_bp:.0f}bp，按信号价已经下不到单", metrics)
    if slip.participation is not None and slip.participation > cfg.max_participation:
        return Verdict("slippage", "block",
                       f"这一单 {amount / 1e4:.0f}万元 占日均成交额 "
                       f"{100 * slip.participation:.1f}%，超过 {100 * cfg.max_participation:.1f}%",
                       metrics)
    if slip.total_bp > cfg.max_slippage_bp:
        return Verdict("slippage", "block",
                       f"预估滑点 {slip.total_bp:.0f}bp（价差 {slip.spread_bp:.0f} + 冲击 "
                       f"{slip.impact_bp:.0f} + 漂移 {slip.drift_bp:.0f}）超过上限 "
                       f"{cfg.max_slippage_bp:.0f}bp", metrics)
    return Verdict("slippage", metrics=metrics)


def model(ctx: RiskContext, cfg, state: dict) -> Verdict:
    """买入风险分：沿用 features/risk_score 的历史剔除闸门（只剔不排，信息不足则放行）。"""
    from stock_selector.features import risk_score

    if not cfg.ml_enabled:
        return Verdict("model", metrics={"skipped": "disabled"})
    if ctx.action != "buy":
        # 模型标签是「今天买入持有 10 日的超额」，卖出侧不在它的论证范围内
        return Verdict("model", metrics={"skipped": "sell_side"})
    if ctx.kline is None or ctx.kline.empty:
        return Verdict("model", metrics={"risk_score": None})
    try:
        rejected, value = risk_score.decide(ctx.kline, cfg.ml_model_dir)
    except RuntimeError as exc:      # 模型缺失是配置故障，必须显式暴露而不是静默放行
        return Verdict("model", "block", f"买入风险分不可用：{exc}", {})
    if value is None:
        return Verdict("model", metrics={"risk_score": None})
    metrics = {"risk_ex10_pp": round(100 * value, 2)}
    if rejected:
        return Verdict("model", "block",
                       f"预测 10 日超额 {100 * value:+.2f}pp 低于剔除阈值，风险分不放行", metrics)
    return Verdict("model", metrics=metrics)


def adx14_rank(ctx: RiskContext, cfg, state: dict) -> Verdict:
    """第 6 条：`adx14`（趋势强度）在**当日全市场**的横截面前 `cfg.adx14_min_pct`% 放行。

    这是六条里唯一分母不是它自己的规则，所以分位由内核预扫后放进 `ctx.cross_pct`
    （见 `risk/cross_section.py`）；规则层自己不拉数，和其余五条一个姿态。

    两种运行模式（`cfg.adx14_mode`）：
    - `shadow`：**永远放行**，但把「这一只会被闸门剔掉吗」写进 metrics。
      shadow 期间要拿的正是"如果剔了会怎样"这个对照，所以它必须在同一批信号上
      留下记录，又绝不能真的改变下单——否则就没有对照了。
    - `live`：低于阈值就 block，理由是中文的、能直接进告警。

    取不到分位（停牌、指标未成形、当日分母不足、sell 方向）一律**放行并留 metric**，
    与其余五条一致：数据不可得时猜一个数，比放过一只票的代价大得多。
    """
    factor = cfg.adx14_factor
    if not cfg.adx14_enabled:
        return Verdict("adx14_rank", metrics={"skipped": "disabled"})
    if ctx.action != "buy":
        # 闸门是在买入候选池内排序，卖出侧不在它的论证范围内
        return Verdict("adx14_rank", metrics={"skipped": "sell_side"})
    pct = ctx.cross_pct
    if pct is None or not np.isfinite(pct):
        return Verdict("adx14_rank", metrics={f"{factor}_pct": None, "skipped": "no_cross_section"})
    metrics = {f"{factor}_pct": round(float(pct), 2),
               "adx14_min_pct": cfg.adx14_min_pct, "adx14_mode": cfg.adx14_mode}
    passed = float(pct) >= cfg.adx14_min_pct
    metrics["adx14_gate"] = "pass" if passed else "reject"
    if passed or cfg.adx14_mode != "live":
        return Verdict("adx14_rank", metrics=metrics)
    return Verdict("adx14_rank", "block",
                   f"{factor} 处于当日全市场前 {100 - cfg.adx14_min_pct:.0f}% 之外"
                   f"（分位 {pct:.0f} < {cfg.adx14_min_pct:.0f}），趋势强度闸门不放行",
                   metrics)


def _last_bar(kline: pd.DataFrame | None) -> dict | None:
    if kline is None or kline.empty or "lu" not in kline.columns:
        return None
    row = kline.iloc[-1]
    value = row.get("lu")
    return {"lu": bool(value) if pd.notna(value) else False}


# 顺序：便宜的、硬约束的在前（成交性 → T+1 → 买入名额 → 流动性），仓位相关的两条必须相邻
# 且 cash_budget 在 position 之后（要用它算出的下单金额）、slippage 之前（滑点按压小后的量估）。
RULES = (tradability, t_plus_1, buy_quota, liquidity, position, cash_budget,
         slippage, model, adx14_rank)
