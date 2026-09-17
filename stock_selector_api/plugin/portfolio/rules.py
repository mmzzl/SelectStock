"""portfolio/rules.py — sell/add/hold 判定规则：默认常量，JSON 可覆盖。

规则表达式与回测 trading_rules 同构（布尔表达式，仅访问 RuleEvaluator 白名单列）。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("portfolio.rules")

ACTION_SELL = "sell"
ACTION_ADD = "add"
ACTION_HOLD = "hold"
RULE_KEYS = (ACTION_SELL, ACTION_ADD, ACTION_HOLD)

# 默认规则（常量；与回测 evaluate_holdings 语义一致：跌破均线→sell，站上均线且未过热→add）
DEFAULT_SELL_RULE = "price <= ma10 * 0.95"
DEFAULT_ADD_RULE = "close >= ma5 * 1.03 and rsi14 <= 65"
DEFAULT_HOLD_RULE = "close >= ma1 and close <= ma99"  # 兜底恒真（无触发时 hold）


def _default_rules() -> dict:
    return {
        ACTION_SELL: DEFAULT_SELL_RULE,
        ACTION_ADD: DEFAULT_ADD_RULE,
        ACTION_HOLD: DEFAULT_HOLD_RULE,
    }


def load_rules() -> dict:
    """读 PLUGIN_RULES_FILE（JSON）覆盖默认规则；文件缺失/{} → 默认常量。"""
    rules = _default_rules()
    path = os.environ.get("PLUGIN_RULES_FILE", "")
    if not path or not Path(path).is_file():
        return rules
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for key in RULE_KEYS:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                rules[key] = value.strip()
    except Exception as exc:  # 规则文件坏了不致命：退回默认
        logger.warning("解析 %s 失败(%s)，使用默认规则", path, exc)
    return rules