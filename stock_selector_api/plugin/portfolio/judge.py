"""portfolio/judge.py — sell/add 判定（hold 兜底）。判定内核复用回测 RuleEvaluator。"""
from __future__ import annotations

from stock_selector.backtest.rule_eval import RuleEvaluator
from stock_selector.backtest.rule_trigger import nearest_price_trigger

REASON = {
    "sell": "跌破近期均线支撑，建议减仓观察",
    "add": "趋势向上且未过热，可考虑小幅加仓",
    "hold": "未触发卖出/加仓条件，继续持有",
}
REASON_DISASTER = "浮亏越过成本灾难线（−{pct:g}%），这条持有的理由已作废，建议减仓止损"

# 2026-10-12 全市场回验（6631 只、72.4 万个判为 hold 的时点、往后 20 个交易日）的标定值。
# 它们不是个股预测，是「hold 这个状态」的整体分布——展示时要按同类基线说，别说成这只票会怎样。
HOLD_WINDOW_DAYS = 20
HOLD_FLIP_PCT = 45.3    # 这些 hold 点里有 45.3% 在 20 日内自己变成 sell
HOLD_TAIL_P5_PCT = 17.5  # 同一批点 20 日收益的 p5 是 −17.5%（尾部，不是均值；均值只有 +0.62%）

_eval = RuleEvaluator()

MAX_REASON_LEN = 128


def judge_holding(row: dict, sell_rule: str, add_rule: str, *, cost_price: float | None = None,
                  price: float | None = None, disaster_stop_pct: float = 0.0) -> tuple[str, str]:
    """返回 (action, action_reason)。灾难线 → sell 优先于 add；均不触发 → hold。

    灾难线排在趋势线之前、且是**唯一一条看成本价的判据**：`price <= ma10*0.95` 描述的是这只票
    自己的形态，它不知道你在什么价位上车。一条 hold 可以形态完好而你已经亏 12%——这时候该动的
    理由是钱，不是形态。`disaster_stop_pct` 传 0 表示不启用（旧的三参调用行为不变）。
    """
    if disaster_stop_pct > 0 and cost_price and cost_price > 0:
        now = float(row["close"]) if price is None else float(price)
        if (now - cost_price) / cost_price * 100 <= -disaster_stop_pct:
            reason = REASON_DISASTER.format(pct=disaster_stop_pct)
            return "sell", reason[:MAX_REASON_LEN]
    if sell_rule and _eval.eval(sell_rule, row):
        return "sell", REASON["sell"][:MAX_REASON_LEN]
    if add_rule and _eval.eval(add_rule, row):
        return "add", REASON["add"][:MAX_REASON_LEN]
    return "hold", REASON["hold"][:MAX_REASON_LEN]


def sell_trigger_price(row: dict, sell_rule: str) -> float | None:
    """卖出规则的价格线；None = 单靠价格涨跌不会翻转这条判定（例如规则只看 RSI）。"""
    found = nearest_price_trigger(sell_rule, row)
    return None if found is None else round(found[0], 2)
