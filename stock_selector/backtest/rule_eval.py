"""trading_rules 条件表达式安全求值器。

规则条件为 Python 布尔表达式字符串，如 `price <= ma10 * 0.8 and adx <= 25 * 1.15`。
求值时仅暴露指标列白名单 + 少量别名（price->close, adx->adx14），关闭 builtins，
避免任意代码执行。
"""

from __future__ import annotations

from ..features.patterns import PATTERN_COLUMNS

# 允许的指标变量（与 parquet 列一一对应）
_ALLOWED_COLUMNS = {
    "open", "high", "low", "close", "volume", "amount",
    "ma5", "ma10", "ma20", "ma60", "ema12", "ema26",
    "macd_dif", "macd_dea", "macd_hist", "rsi14",
    "kdj_k", "kdj_d", "kdj_j", "wr14", "atr14",
    "boll_upper", "boll_mid", "boll_lower", "obv", "roc10",
    "vol_ma5", "last_close", "adx14", "adx",
    "pct", "vol_ratio", "pos60",
    # 形态/量能列由 patterns.py 定义，直接从它导入避免两边清单漂移
    *PATTERN_COLUMNS,
}

_ALIASES = {
    "price": "close",
    "adx": "adx14",
}


class RuleEvaluator:
    """对单行指标数据求值规则条件，返回 bool。"""

    def eval(self, condition: str, row: dict) -> bool:
        if not condition or not isinstance(condition, str):
            return False
        try:
            ns: dict = {}
            aliased = False
            for key, value in row.items():
                col = _ALIASES.get(key)
                if col is None:
                    col = key
                if col in _ALLOWED_COLUMNS and col not in ns:
                    ns[col] = float(value)
            # 显式补别名映射（即使 row 用的是别名而不是基础列）
            for alias, base in _ALIASES.items():
                if alias not in ns and base in ns:
                    ns[alias] = ns[base]
            result = eval(condition, {"__builtins__": {}}, ns)
            return bool(result)
        except Exception:
            return False
