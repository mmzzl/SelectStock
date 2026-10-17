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

# 台账（scripts/build_judgement_ledger.py → data/judgement_ledger/posterior.parquet）的
# 2026-10-13 全量快照，只在台账文件读不到时当兜底常数用（正常路径由 portfolio/posterior.py 直读表）。
# 它们不是个股预测，是「hold 这个状态」的整体分布——展示时要按同类基线说，别说成这只票会怎样。
HOLD_WINDOW_DAYS = 20
HOLD_FLIP_PCT = 46.1     # 这些 hold 点里有 46.1% 在 20 日内自己变成 sell
HOLD_TAIL_P5_PCT = 17.8  # 同一批点 20 日收益的 p5 是 −17.8%（尾部，不是均值；均值只有 +0.15%）

_eval = RuleEvaluator()

MAX_REASON_LEN = 128


def judge_holding(row: dict, sell_rule: str, add_rule: str, *, cost_price: float | None = None,
                  price: float | None = None, disaster_stop_pct: float = 0.0,
                  sell_rules: list | None = None, add_rules: list | None = None) -> tuple[str, str]:
    """返回 (action, action_reason)。灾难线 → sell 优先于 add；均不触发 → hold。

    灾难线排在趋势线之前、且是**唯一一条看成本价的判据**：`price <= ma10*0.95` 描述的是这只票
    自己的形态，它不知道你在什么价位上车。一条 hold 可以形态完好而你已经亏 12%——这时候该动的
    理由是钱，不是形态。`disaster_stop_pct` 传 0 表示不启用（旧的三参调用行为不变）。

    `sell_rules`/`add_rules` 是规则集里的逐条形（`[{'label','expr'}...]`）：给了就逐条求值，
    文案写**第一条命中的那条规则**——「建议卖出」必须说得出是哪条警报响的，否则用户没法核对，
    而 `sell_rule`（OR 串）仍然单独用于反算卖点价。两条判据走同一个 RuleEvaluator，
    不会出现「理由说跌破均线、判定其实来自 RSI」。
    """
    if disaster_stop_pct > 0 and cost_price and cost_price > 0:
        now = float(row["close"]) if price is None else float(price)
        if (now - cost_price) / cost_price * 100 <= -disaster_stop_pct:
            reason = REASON_DISASTER.format(pct=disaster_stop_pct)
            return "sell", reason[:MAX_REASON_LEN]
    hit = first_hit(sell_rules, row)
    if hit is not None:
        return "sell", _rule_reason("sell", hit)
    # `sell_rules is None` 才是「老式单串调用」；空列表是「这一侧的规则被关掉了」，
    # 用 `not sell_rules` 判会让关掉的规则集顺着 sell_rule 那根 OR 串复活。
    if sell_rule and sell_rules is None and _eval.eval(sell_rule, row):
        return "sell", REASON["sell"][:MAX_REASON_LEN]
    hit = first_hit(add_rules, row)
    if hit is not None:
        return "add", _rule_reason("add", hit)
    if add_rule and add_rules is None and _eval.eval(add_rule, row):
        return "add", REASON["add"][:MAX_REASON_LEN]
    return "hold", REASON["hold"][:MAX_REASON_LEN]


def first_hit(items: list | None, row: dict):
    """规则集里第一条触发的 label（没写 label 就用表达式）；没给列表或都没触发 → None。"""
    for item in items or []:
        expr = item.get("expr") if isinstance(item, dict) else item
        if isinstance(expr, str) and expr and _eval.eval(expr, row):
            return (item.get("label") if isinstance(item, dict) else "") or expr
    return None


def _rule_reason(name: str, label: str) -> str:
    label = str(label).strip()[:40]
    text = (f"触发卖出规则「{label}」，建议减仓观察" if name == "sell"
            else f"触发加仓规则「{label}」，可考虑小幅加仓")
    return text[:MAX_REASON_LEN]


def sell_trigger_price(row: dict, sell_rule: str, sell_rules: list | None = None) -> float | None:
    """卖出规则的价格线；None = 单靠价格涨跌不会翻转这条判定（例如规则只看 RSI）。

    多条规则时取**离当前价最近**的那条触发线：页面上那是「再跌多少就报警」的实际边界，
    而各条规则各自的线在文案里由 `action_reason` 指明是哪一条。
    """
    candidates = [i.get("expr") for i in (sell_rules or []) if isinstance(i, dict)] or [sell_rule]
    found = [f for f in (nearest_price_trigger(e, row) for e in candidates if e) if f]
    if not found:
        return None
    base = float(row.get("close") or 0.0)
    nearest = min(found, key=lambda f: abs(f[0] - base)) if base > 0 else found[0]
    return round(nearest[0], 2)
