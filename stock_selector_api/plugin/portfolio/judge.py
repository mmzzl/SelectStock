"""portfolio/judge.py — sell/add 判定（hold 兜底）。判定内核复用回测 RuleEvaluator。"""
from __future__ import annotations

from stock_selector.backtest.rule_eval import RuleEvaluator

REASON = {
    "sell": "跌破近期均线支撑，建议减仓观察",
    "add": "趋势向上且未过热，可考虑小幅加仓",
    "hold": "未触发卖出/加仓条件，继续持有",
}

_eval = RuleEvaluator()

MAX_REASON_LEN = 128


def judge_holding(row: dict, sell_rule: str, add_rule: str) -> tuple[str, str]:
    """返回 (action, action_reason)。sell 优先于 add；均不触发 → hold。"""
    if sell_rule and _eval.eval(sell_rule, row):
        return "sell", REASON["sell"][:MAX_REASON_LEN]
    if add_rule and _eval.eval(add_rule, row):
        return "add", REASON["add"][:MAX_REASON_LEN]
    return "hold", REASON["hold"][:MAX_REASON_LEN]
