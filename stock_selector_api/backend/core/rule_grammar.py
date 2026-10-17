"""core/rule_grammar.py — 惰性拿到内核的规则文法（唯一真源在 stock_selector/backtest/rule_expr.py）。

为什么要惰性：那份模块会连带 import pandas/numpy（指标列白名单来自 features.patterns），
而「后端零计算」这条线要求 API 进程启动时不拖内核依赖。只有真的在校验/拼规则表达式时才把它
带进来，一次之后模块缓存复用。

为什么要在 API 侧用文法：结构化条件是页面填的，表达式串在这里拼出来并当场校验——存进去之后
才发现语法错的那条规则，在线上等于**永远不触发**（RuleEvaluator 静默返回 False），
比报错严重得多。
"""
from __future__ import annotations

import sys

from config import settings

_mod = None
_gates = None


def rule_expr():
    global _mod
    if _mod is None:
        root = str(settings.backtest_root)
        if root not in sys.path:
            sys.path.insert(0, root)
        from stock_selector.backtest import rule_expr as mod

        _mod = mod
    return _mod


def compile_conditions(conditions: list[dict]) -> str:
    """结构化条件（同一条规则内 AND）→ 表达式串，语法错抛 RuleSyntaxError。"""
    return rule_expr().build_expression(conditions)


def check_expr(expr: str) -> list[str]:
    """校验表达式串，返回它用到的字段；不合法抛 RuleSyntaxError。"""
    return rule_expr().validate(expr)


def trade_gates():
    """逐笔盈亏门槛的唯一真源：`stock_selector/backtest/rule_gates.py`。

    页面默认筛法（「只看笔数达标的卖法」）必须跟判定用同一个数，写第二份就会漂移；
    和文法一样惰性加载，API 进程启动时不拖 numpy。
    """
    global _gates
    if _gates is None:
        root = str(settings.backtest_root)
        if root not in sys.path:
            sys.path.insert(0, root)
        from stock_selector.backtest import rule_gates as mod

        _gates = mod
    return _gates
