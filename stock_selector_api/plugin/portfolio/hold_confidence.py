"""portfolio/hold_confidence.py — 「持有可信度」直读规则集全市场回放的产物。

方案 B 的口径（见 `scripts/ruleset_posterior.py`）：不预测个股收益，只统计**历史上「全部卖出
规则同时都不触发」这个状态**的事后分布——后 20 个交易日收盘价没有跌破 −3% 的样本占比。
所以它是状态频率，不是「这只票接下来 20 天不会跌」的承诺，文案必须把 `base_pct`（同日全体
基准）和 `lift` 一起说出去：2026-10-16 那次抽样实测 lift 只有 0.988（t=−3.85），也就是
「什么都没响」这个状态比随机挑一只票还略差一点。只印 34.9% 这种绝对数就是在骗人。

三条退化路径都要走到，不能让诊断整页没有判定：
1. 回放文件不存在 → 只说「还没跑过回放」，不给数字；
2. 回放对应的卖出规则和当前生效规则集不是同一套 → 数字属于另一条规则，不给；
3. 联合状态样本不足（N<200 或覆盖交易日<30）→ 「样本不足，无法评估」，不给数字。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from portfolio.judge import first_hit

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_FILE = REPO_ROOT / "data" / "rules" / "active_posterior.json"

# lift 离 1 不到这个幅度，或配对 t 压不住噪声，就明说「无区分度」而不是给个高低档
FLAT_LIFT_BAND = 0.03
MIN_T = 2.0

_cache: dict | None = None
_cache_key: tuple = ()


def _path() -> Path:
    return Path(os.environ.get("RULESET_POSTERIOR_FILE") or DEFAULT_FILE)


def load() -> dict | None:
    """按 (路径, mtime, size) 缓存：后台重算回放后不需要重启插件就能读到新数。"""
    global _cache, _cache_key
    path = _path()
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), stat.st_mtime, stat.st_size)
    if _cache is None or _cache_key != key:
        try:
            _cache = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:            # 写一半/坏文件不能让诊断报 500
            logger.warning("回放产物读取失败 %s：%s", path, exc)
            _cache = None
        _cache_key = key
    return _cache


def _sell_exprs(payload: dict) -> set[str]:
    return {str(r.get("expr") or "").strip() for r in payload.get("sell_rules") or []
            if str(r.get("expr") or "").strip()}


def _matches(payload: dict, rules: dict) -> bool:
    """回放是不是「当前这一套卖出规则」：比表达式，不比 rule_set_id。

    id 会在同一条规则被删掉重建时变化，而联合状态只由卖出规则的表达式集合决定；
    比表达式才不会把旧规则集的数印在新规则集上。
    """
    live = {str(i.get("expr") or "").strip() for i in rules.get("sell_rules") or []
            if isinstance(i, dict) and str(i.get("expr") or "").strip()}
    played = _sell_exprs(payload)
    return bool(live) and bool(played) and live == played


def evaluate(action: str, row: dict, rules: dict) -> dict:
    """返回要并进诊断结果的字段：`hold_confidence_*` + 一行可直接排版的文案。"""
    payload = load()
    if payload is None:
        return {"hold_confidence_pct": None,
                "hold_confidence_text": "持有可信度：这套规则还没跑过全市场回放"}
    if not _matches(payload, rules):
        return {"hold_confidence_pct": None,
                "hold_confidence_text": "持有可信度：回放对应的是另一套卖出规则，暂不给数"}

    c = payload.get("contract") or {}
    horizon = int(c.get("horizon_days") or 20)
    flat_th = float(c.get("flat_threshold_pct") or 3.0)

    if action in ("sell", "add"):
        # 判成 sell/add 时，「全部不触发」这个状态压根不成立，印它是自相矛盾；
        # 该印的是**响的那条规则**自己的事后表现
        return _rule_line(payload, rules, row, action, c, horizon, flat_th)

    joint = payload.get("joint_hold") or {}
    pct = payload.get("hold_confidence_pct")
    if pct is None:
        return {"hold_confidence_pct": None,
                "hold_confidence_text": f"持有可信度：{payload.get('hold_confidence_note') or '样本不足，无法评估'}"}
    out = {
        "hold_confidence_pct": pct,
        "hold_confidence_base_pct": joint.get("base_pct"),
        "hold_confidence_lift": joint.get("lift"),
        "hold_confidence_n": joint.get("n"),
        "hold_confidence_days": joint.get("days"),
        "hold_confidence_level": _level(joint),
        "hold_confidence_window": f"T+{horizon}",
        "hold_confidence_rule_set": payload.get("name") or "",
        "hold_confidence_at": payload.get("computed_at") or "",
    }
    out["hold_confidence_text"] = _hold_text(out, joint, horizon, flat_th)
    return out


def _hold_text(info: dict, joint: dict, horizon: int, flat_th: float) -> str:
    """一行话：绝对数 + 基准 + 区分度 + 样本量，四样缺一不可。"""
    base = joint.get("base_pct")
    lift = joint.get("lift")
    bits = [f"持有可信度 {info['hold_confidence_pct']:.1f}%"
            f"（后{horizon}日未跌超{flat_th:g}%）"]
    if base is not None and lift is not None:
        bits.append(f"全体基准 {base:.1f}%·lift {lift:.2f}·{info['hold_confidence_level']}")
    t = joint.get("vs_base_t")
    if t is not None:
        bits.append(f"配对 t={t:+.1f}")
    bits.append(f"N={int(info.get('hold_confidence_n') or 0):,}/{info.get('hold_confidence_days') or 0}日")
    tail8, tail8_lift = joint.get("tail8_pct"), joint.get("tail8_lift")
    if tail8 is not None:
        bits.append(f"20日内跌超8% {tail8:.1f}%"
                    + (f"（lift {tail8_lift:.2f}）" if tail8_lift is not None else ""))
    return "｜".join(bits)


def _rule_line(payload: dict, rules: dict, row: dict, action: str, c: dict,
               horizon: int, flat_th: float) -> dict:
    """判成 sell/add 时印**那条响了的规则**自己的事后表现：兑现率/卖飞率/尾部/样本量。

    卖出规则的「兑现」= 触发后 20 日收盘真的跌过 −3%（就是 100 −「未跌超」占比），「卖飞」=
    涨过 +3%；加仓规则反过来看，兑现按涨过算，并且必须带上 exc20_pp——它是相对**同日
    「全部不触发」那些点**的配对超额，加仓线到底比继续持有好在哪，只有这个数说得出。
    """
    side = "sell" if action == "sell" else "add"
    cn = "卖出" if side == "sell" else "加仓"
    items = payload.get(f"{side}_rules") or []
    label = first_hit(rules.get(f"{side}_rules"), row)
    if not label:
        # 判成 sell 但没有一条形态规则在响 ⇒ 只有成本灾难线会这样（它看的是成本价，
        # 规则集回放里没有这一项，因为回放是全市场的、不知道你的成本）
        return _no_pct(f"本次{cn}由成本灾难线（−{float(rules.get('disaster_stop_pct') or 0):g}%）触发，"
                       "形态规则没有一条在响")
    stats = _find_rule(items, label)
    name = f"{cn}规则「{str(label or '')[:16]}」事后："
    rng = (payload.get("universe") or {}).get("range") or ["", ""]
    if stats is None:
        return _no_pct(name + "这条规则不在回放里（规则集改过，需要重跑回放）")
    if not stats.get("n"):
        return _no_pct(name + f"在回放窗口（{rng[0]}→{rng[-1]}）里从未触发")
    if stats.get("flat_pct") is None:
        return _no_pct(name + f"样本不足（N={int(stats.get('n') or 0)}），不给数")

    fly_th = float(c.get("fly_threshold_pct") or 3.0)
    hit_pct = (round(100 - float(stats["flat_pct"]), 1) if side == "sell"
               else round(float(stats["fly_pct"] or 0.0), 1))
    if side == "sell":
        bits = [name + f"兑现(跌超{flat_th:g}%) {hit_pct:.1f}%",
                f"卖飞(涨过{fly_th:g}%) {stats['fly_pct']:.1f}%"]
    else:
        bits = [name + f"兑现(涨过{fly_th:g}%) {hit_pct:.1f}%"]
        exc = stats.get("exc20_pp")
        if exc is not None:
            bits.append(f"20日比同日「未触发」{exc:+.2f}pp")
    tail8, lift, base_tail = stats.get("tail8_pct"), stats.get("tail8_lift"), stats.get("base_tail8_pct")
    if tail8 is not None:
        tail = f"跌超8% {tail8:.1f}%"
        if lift is not None and base_tail is not None:
            tail += f"（基准 {base_tail:.1f}%，lift {lift:.2f}）"
        bits.append(tail)
    if stats.get("median_ret_pct") is not None:
        bits.append(f"{horizon}日中位 {stats['median_ret_pct']:+.2f}%")
    bits.append(f"N={int(stats.get('n') or 0):,}/{stats.get('days') or 0}日")
    return {"hold_confidence_pct": None,
            "hold_confidence_level": None,
            "hold_confidence_rule": {"side": side, "label": label, "hit_pct": hit_pct,
                                     "n": stats.get("n"), "days": stats.get("days"),
                                     "fly_pct": stats.get("fly_pct"),
                                     "exc20_pp": stats.get("exc20_pp"),
                                     "tail8_pct": tail8, "tail8_lift": lift,
                                     "median_ret_pct": stats.get("median_ret_pct")},
            "hold_confidence_rule_set": payload.get("name") or "",
            "hold_confidence_at": payload.get("computed_at") or "",
            "hold_confidence_text": "｜".join(bits)}


def _no_pct(text: str) -> dict:
    """「这条数我给不出来」的统一形状：不给 pct，只给一句说得清原因的话。"""
    return {"hold_confidence_pct": None, "hold_confidence_level": None,
            "hold_confidence_text": text}

def _find_rule(items: list[dict], label) -> dict | None:
    """按 label 找；`first_hit` 对没写标签的规则返回的是表达式本身，所以两边都比。"""
    want = str(label or "").strip()
    for item in items:
        if want and want in (str(item.get("label") or "").strip(),
                             str(item.get("expr") or "").strip()):
            return item
    return None


def _level(joint: dict) -> str:
    """只按「相对同日全体的差」定档，不按绝对数——绝对数会被行情水位带着走。"""
    lift, t = joint.get("lift"), joint.get("vs_base_t")
    if lift is None:
        return "无基准"
    if abs(lift - 1.0) < FLAT_LIFT_BAND or (t is not None and abs(float(t)) < MIN_T):
        return "无区分度"
    return "高于基准" if lift > 1.0 else "低于基准"
