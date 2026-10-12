"""risk/rules.py — 五条风控规则：每条只回答「这个方向这一单能不能成交、值不值得成交」。

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


def _last_bar(kline: pd.DataFrame | None) -> dict | None:
    if kline is None or kline.empty or "lu" not in kline.columns:
        return None
    row = kline.iloc[-1]
    value = row.get("lu")
    return {"lu": bool(value) if pd.notna(value) else False}


RULES = (tradability, liquidity, position, slippage, model)
