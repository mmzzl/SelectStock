"""rule_expr：结构化条件 ⇄ 表达式串 ⇄ 向量化求值，三条路必须同口径。

这里最要紧的一条是**向量化结果和 `RuleEvaluator` 逐行 eval 完全一致**：台账/回放用它、
线上判定用 RuleEvaluator，两边一旦分家，页面上就会出现「理由是 A 规则、判定来自 B 规则」
这种不报错的偏差。
"""

import numpy as np
import pandas as pd
import pytest

from stock_selector.backtest import rule_expr
from stock_selector.backtest.rule_eval import RuleEvaluator

ROWS = {
    "close": [10.0, 9.4, 11.0, np.nan, 12.0, 10.0],
    "ma5": [9.8, 9.9, 10.1, 10.0, 10.2, 9.7],
    "ma10": [10.0, 9.9, 10.0, 10.0, 10.5, 10.0],
    "rsi14": [55.0, 48.0, 91.0, 60.0, 20.0, 65.0],
    "atr14": [0.3, 0.4, 0.2, 0.5, 0.6, 0.4],
    "ma20": [10.2, 9.8, 10.5, 10.0, 11.0, 9.6],
    "boll_lower": [9.0, 9.1, 9.2, 9.3, 9.4, 9.5],
}
COLS = {k: np.array(v, dtype=float) for k, v in ROWS.items()}
_eval = RuleEvaluator()


def _rows():
    return [dict(zip(ROWS, vals)) for vals in zip(*ROWS.values())]


def _rowwise(expr):
    return [bool(_eval.eval(expr, r)) for r in _rows()]


# ------------------------------------------------------------------ 结构化 → 串

def test_render_value_ref_and_volatility_normalisation():
    assert rule_expr.render({"field": "rsi14", "op": ">", "value": 90}) == "rsi14 > 90"
    assert rule_expr.render({"field": "price", "op": "<=", "ref": "ma10",
                             "coef": 0.95}) == "close <= ma10 * 0.95"
    # 波动归一：固定 5% 换成「1 个 ATR」，右值是两个列项之和
    assert rule_expr.render({"field": "close", "op": "<=", "ref": "ma10", "coef": 1.0,
                             "plus": {"field": "atr14", "sign": "-", "coef": 1.0}}) \
        == "close <= ma10 - atr14"


def test_render_rejects_unsupported_shape():
    with pytest.raises(rule_expr.RuleSyntaxError):
        rule_expr.render({"field": "close", "op": "==", "value": 1})
    with pytest.raises(rule_expr.RuleSyntaxError):
        rule_expr.render({"field": "close", "op": "<="})          # 既没 ref 也没 value


def test_build_expression_ands_conditions_and_join_rules_ors_them():
    conds = [{"field": "rsi14", "op": "<", "value": 70},
             {"field": "close", "op": ">", "ref": "ma20"}]
    expr = rule_expr.build_expression(conds)
    assert expr == "rsi14 < 70 and close > ma20"     # 系数 1 不写出来
    assert rule_expr.join_rules([expr, "rsi14 > 90"]) == f"{expr} or rsi14 > 90"
    assert rule_expr.join_rules(["", expr]) == expr      # 空规则不参与 OR
    assert rule_expr.build_expression([]) == ""


# ------------------------------------------------------------------ 校验

def test_validate_returns_canonical_columns_not_aliases():
    assert rule_expr.validate("price <= ma10 * 0.95 and adx > 20") == ["adx14", "close", "ma10"]


@pytest.mark.parametrize("bad", [
    "close == 10",                       # 相等比较：两条求值路径必然分家
    "(close < ma10)",                    # 括号不支持
    "max(close) < 1",                    # 函数
    "not (close < ma10)",                # 取非
    "mystery > 1",                       # 白名单外的字段
    "close <",                           # 缺右值
    "close < ma10 and",                  # 多余的 and
    "close < ma10 or or close > ma5",    # 多余的 or
    "close @ ma10 < 1",                  # 不认识的运算符
])
def test_validate_rejects(bad):
    with pytest.raises(rule_expr.RuleSyntaxError):
        rule_expr.validate(bad)


# ------------------------------------------------------------------ 两条路径一致

@pytest.mark.parametrize("expr", [
    "close <= ma10 * 0.95",
    "rsi14 > 90",
    "close <= ma10 - atr14 * 1.0",
    "close > ma20 and rsi14 < 70",
    "price <= ma10 * 0.95 or rsi14 > 90",
    # and 比 or 绑得紧：Python 的 `a or b and c` 是 `a or (b and c)`，向量化必须一样
    "rsi14 > 90 or close < ma10 and ma5 > ma10",
    "close < ma5 and rsi14 < 60 or boll_lower > 9.1 and atr14 < 0.5",
    "close / 1.0 <= ma10 * 1 - atr14 * 0.5",
])
def test_matrix_matches_rowwise_evaluator(expr):
    got = list(rule_expr.eval_matrix(expr, COLS).astype(bool))
    assert got == _rowwise(expr), expr


def test_matrix_and_evaluator_both_treat_nan_as_not_triggered():
    """第 4 行 close 是 NaN：两边都得是「没触发」，而不是抛异常或判成 True。"""
    for expr in ("close <= ma10", "close > ma10", "close >= 0"):
        assert not bool(rule_expr.eval_matrix(expr, COLS)[3])
        assert _rowwise(expr)[3] is False


def test_matrix_reports_missing_column_instead_of_silently_false():
    with pytest.raises(rule_expr.RuleSyntaxError):
        rule_expr.eval_matrix("close <= ma60 * 0.9", COLS)
