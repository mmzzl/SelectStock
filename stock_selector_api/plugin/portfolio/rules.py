"""portfolio/rules.py — sell/add/hold 判定规则：默认常量 + 管理页面「应用」的规则集。

规则表达式与回测 trading_rules 同构（布尔表达式，仅访问 RuleEvaluator 白名单列），文法由
`stock_selector/backtest/rule_expr.py` 单一定义——判定、台账、回放三边共用同一套解析。

真源优先级（找不到就逐级退化，绝不因为规则文件坏掉而不给判定）：
1. `PLUGIN_RULES_FILE`（测试/临时指定）；
2. `data/rules/active.json`（后台管理页「应用」写出的那份，带 rule_set_id 与逐条标签）；
3. 本模块的默认常量。

**默认常量的出处要查清再用：这两条都没有标定过。**
- `price <= ma10 * 0.95` 最早是初始提交里 `tests/test_backtest_rules.py` 的**测试夹具**
  （只为验证 RuleEvaluator 能跑通），M2（2026-09-17 计划文档）原样抄成了默认卖出规则；
- `close >= ma5 * 1.03 and rsi14 <= 65` 连夹具都没有，是那份计划里现编的。
所以 0.95 / 1.03 / 65 三个常数**不是标定值，是占位值**。改判据请走管理页面建规则集→回测→
`scripts/ruleset_posterior.py` 看数，别直接改这里。

多条卖出规则的语义（页面「应用」的那套）：`sell` 串是各条规则的 OR，命中任意一条即建议卖出；
「持有可信度」量的则是**全部规则都不触发**这个联合状态（见 `scripts/ruleset_posterior.py`）。
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

REPO_ROOT = Path(__file__).resolve().parents[3]
ACTIVE_RULES_FILE = REPO_ROOT / "data" / "rules" / "active.json"

DEFAULT_SELL_RULE = "price <= ma10 * 0.95"
DEFAULT_ADD_RULE = "close >= ma5 * 1.03 and rsi14 <= 65"
DEFAULT_HOLD_RULE = "close >= ma1 and close <= ma99"  # 兜底恒真（无触发时 hold）

# 灾难线：浮亏超过这个百分比就作废「持有」。全市场回验（72.4 万个判为 hold 的时点、20 日窗口）
# 显示它 90% 与趋势线重叠、期望上收 −0.08pp 的保费——它是砍尾部的保险，不是预测，
# 所以阈值给到 −8%（用户定的风险口径）， −5% 会把新增告警放到 12.7% 且每周期拖 −0.38pp。
DEFAULT_DISASTER_STOP_PCT = 8.0
MIN_DISASTER_STOP_PCT = 1.0
MAX_DISASTER_STOP_PCT = 30.0


def _default_rules() -> dict:
    return {
        ACTION_SELL: DEFAULT_SELL_RULE,
        ACTION_ADD: DEFAULT_ADD_RULE,
        ACTION_HOLD: DEFAULT_HOLD_RULE,
        "sell_rules": [{"label": "默认趋势线", "expr": DEFAULT_SELL_RULE}],
        "add_rules": [{"label": "默认加仓线", "expr": DEFAULT_ADD_RULE}],
        "disaster_stop_pct": DEFAULT_DISASTER_STOP_PCT,
        "rule_set_id": None,
        "name": "默认规则（未标定）",
        "source": "default",
    }


def _candidate_paths() -> list[Path]:
    env = os.environ.get("PLUGIN_RULES_FILE", "")
    if env:
        return [Path(env)]                  # 显式指定就是显式指定，不做回退猜测
    return [ACTIVE_RULES_FILE]


def load_rules() -> dict:
    """读当前生效的规则集；文件缺失/坏/形状不认识 → 默认常量（并记一条 warning）。"""
    rules = _default_rules()
    path = next((p for p in _candidate_paths() if p.is_file()), None)
    if path is None:
        return rules
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("规则文件 %s 解析失败(%s)，退回默认规则", path, exc)
        return rules
    out = dict(rules)
    out["source"] = str(path)
    out["rule_set_id"] = data.get("rule_set_id")
    out["name"] = str(data.get("name") or "已应用规则集")
    for key in RULE_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()
    for key in ("sell_rules", "add_rules"):
        items = _normalize_items(data.get(key))
        if items:
            out[key] = items
            out[_LEGACY[key]] = join_or(items)
    stop = data.get("disaster_stop_pct")
    if isinstance(stop, (int, float)) and not isinstance(stop, bool):
        out["disaster_stop_pct"] = min(max(float(stop), MIN_DISASTER_STOP_PCT),
                                       MAX_DISASTER_STOP_PCT)
    return out


_LEGACY = {"sell_rules": ACTION_SELL, "add_rules": ACTION_ADD}


def _normalize_items(raw) -> list[dict]:
    """接受 [{'label','expr'}...] 或 ['expr'...]；任何非字符串项丢掉，不猜。"""
    if not isinstance(raw, list):
        return []
    from stock_selector.backtest.rule_expr import RuleSyntaxError, validate

    out = []
    for i, item in enumerate(raw):
        expr = item.get("expr") if isinstance(item, dict) else item
        if not isinstance(expr, str) or not expr.strip():
            continue
        try:
            validate(expr.strip())
        except RuleSyntaxError as exc:
            logger.warning("规则集第 %d 条表达式不合法，整条丢弃：%s", i + 1, exc)
            continue
        label = (item.get("label") if isinstance(item, dict) else None) or f"规则{i + 1}"
        out.append({"label": str(label)[:40], "expr": expr.strip()})
    return out


def join_or(items: list[dict]) -> str:
    from stock_selector.backtest.rule_expr import join_rules

    return join_rules([i["expr"] for i in items])
