"""规则集落地：多规则判定（judge）+ 规则文件真源（rules）+ 方案 B 的持有可信度（hold_confidence）。

这三个模块是「页面点应用 → 诊断/持仓立刻按新规则判、可信度按新规则算」的链路两端，
所以用例锁的是**退化路径**：规则文件坏掉、回放对应的是另一套规则、样本不足——
任何一种都必须照样给判定，只是不给可信度数字。
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# 插件模块用自己的目录当 import 根（`from portfolio.judge import ...`），和 8001/8002 起进程时一致
sys.path.insert(0, str(ROOT / "stock_selector_api" / "plugin"))

from portfolio import hold_confidence, rules as rules_mod          # noqa: E402
from portfolio.judge import judge_holding, sell_trigger_price      # noqa: E402

ROW = {"close": 9.40, "ma5": 9.90, "ma10": 10.00, "ma20": 10.20, "rsi14": 48.0, "atr14": 0.30}

SELL_RULES = [
    {"label": "跌破10日线5%", "expr": "close <= ma10 * 0.95"},
    {"label": "RSI超买", "expr": "rsi14 > 90"},
]


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    monkeypatch.setattr(hold_confidence, "_cache", None)
    monkeypatch.setattr(hold_confidence, "_cache_key", ())


# ------------------------------------------------------------------ 规则文件

def test_load_rules_from_active_file_exposes_list_and_legacy_string(tmp_path, monkeypatch):
    import json

    f = tmp_path / "active.json"
    f.write_text(json.dumps({"rule_set_id": "rs-1", "name": "我的规则",
                             "sell_rules": SELL_RULES,
                             "add_rules": [{"label": "趋势", "expr": "close > ma20"}],
                             "disaster_stop_pct": 12.0}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("PLUGIN_RULES_FILE", str(f))
    got = rules_mod.load_rules()
    assert got["rule_set_id"] == "rs-1" and got["name"] == "我的规则"
    assert got["sell"] == "close <= ma10 * 0.95 or rsi14 > 90"    # 旧的单串消费方不能断
    assert got["add"] == "close > ma20"
    assert [i["label"] for i in got["sell_rules"]] == ["跌破10日线5%", "RSI超买"]
    assert got["disaster_stop_pct"] == 12.0


def test_load_rules_drops_unparsable_rule_but_keeps_the_rest(tmp_path, monkeypatch):
    import json

    f = tmp_path / "active.json"
    f.write_text(json.dumps({"sell_rules": [{"label": "坏", "expr": "close == ma10"},
                                            {"label": "好", "expr": "rsi14 > 90"}]}),
                 encoding="utf-8")
    monkeypatch.setenv("PLUGIN_RULES_FILE", str(f))
    got = rules_mod.load_rules()
    assert got["sell_rules"] == [{"label": "好", "expr": "rsi14 > 90"}]
    assert got["sell"] == "rsi14 > 90"


def test_load_rules_missing_file_falls_back_to_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("PLUGIN_RULES_FILE", str(tmp_path / "nope.json"))
    got = rules_mod.load_rules()
    assert got["source"] == str(tmp_path / "nope.json") or got["rule_set_id"] is None
    assert got["rule_set_id"] is None and got["sell"] == rules_mod.DEFAULT_SELL_RULE


# ------------------------------------------------------------------ 多规则判定

def test_judge_reports_which_rule_fired():
    action, reason = judge_holding(ROW, "", "", sell_rules=SELL_RULES)
    assert action == "sell"
    assert "跌破10日线5%" in reason          # 「建议卖出」必须说得出哪条警报响的


def test_disaster_line_beats_every_rule_and_add_wins_over_nothing():
    action, reason = judge_holding(dict(ROW, close=10.40, rsi14=20.0), "", "",
                                   cost_price=12.0, disaster_stop_pct=8.0,
                                   sell_rules=SELL_RULES,
                                   add_rules=[{"label": "趋势", "expr": "close > ma10"}])
    assert action == "sell" and "灾难线" in reason
    action, reason = judge_holding(dict(ROW, close=10.40, rsi14=20.0), "", "",
                                   cost_price=10.5, disaster_stop_pct=8.0,
                                   sell_rules=SELL_RULES,
                                   add_rules=[{"label": "趋势", "expr": "close > ma10"}])
    assert action == "add" and "趋势" in reason
    action, _ = judge_holding(dict(ROW, close=10.40, rsi14=20.0), "", "",
                              sell_rules=SELL_RULES)
    assert action == "hold"


def test_legacy_single_string_call_still_works():
    action, reason = judge_holding(ROW, rules_mod.DEFAULT_SELL_RULE, "")
    assert action == "sell" and "跌破近期均线支撑" in reason


def test_sell_trigger_price_takes_the_nearest_line_among_rules():
    # 两条规则各自的线：ma10*0.95=9.5 和 RSI 那条不含价格项 → 只可能给 9.5
    assert sell_trigger_price(ROW, "", SELL_RULES) == 9.5
    # 规则只看 RSI 时没有价格线（None），而不是硬凑一个价
    assert sell_trigger_price(ROW, "", SELL_RULES[1:]) is None


# ------------------------------------------------------------------ 持有可信度

def _payload(**over):
    p = {
        "version": 1, "rule_set_id": "rs-1", "name": "我的规则",
        "computed_at": "2026-10-16 07:00:00",
        "contract": {"horizon_days": 20, "flat_threshold_pct": 3.0, "fly_threshold_pct": 3.0,
                     "tail_threshold_pct": 8.0, "min_samples": 200, "min_days": 30,
                     "meaning": "状态频率"},
        "universe": {"days": 2690, "files": 33, "st_excluded": True,
                     "range": ["2014-12-30", "2026-10-15"]},
        "base": {"n": 122654},
        "joint_hold": {"n": 40106, "days": 2690, "flat_pct": 34.03, "fly_pct": 35.16,
                       "tail8_pct": 44.65, "tail8_lift": 1.01, "flip_sell_20_pct": 73.2,
                       "median_ret_pct": -0.75, "base_pct": 34.42, "lift": 0.988,
                       "vs_base_t": -2.58, "enough": True},
        "sell_rules": [{"label": "跌破10日线5%", "expr": "close <= ma10 * 0.95",
                        "n": 5590, "days": 1647, "flat_pct": 25.8, "fly_pct": 35.0,
                        "tail8_pct": 57.0, "tail8_lift": 1.23, "base_tail8_pct": 46.5,
                        "median_ret_pct": 0.25, "exc20_pp": -0.26, "enough": True},
                       {"label": "RSI超买", "expr": "rsi14 > 90", "n": 74, "days": 67,
                        "flat_pct": 30.6, "fly_pct": 40.7, "tail8_pct": 61.2,
                        "tail8_lift": 1.49, "base_tail8_pct": 41.1, "median_ret_pct": -2.75,
                        "exc20_pp": 5.48, "enough": True}],
        "add_rules": [{"label": "趋势", "expr": "close > ma20", "n": 20699, "days": 2556,
                       "flat_pct": 32.0, "fly_pct": 35.0, "tail8_pct": 46.6,
                       "tail8_lift": 1.05, "base_tail8_pct": 44.5, "median_ret_pct": -0.75,
                       "exc20_pp": -0.41, "enough": True}],
        "hold_confidence_pct": 34.03,
    }
    p.update(over)
    return p


_RULES = {"rule_set_id": "rs-1", "name": "我的规则", "sell_rules": SELL_RULES,
          "add_rules": [{"label": "趋势", "expr": "close > ma20"}],
          "disaster_stop_pct": 8.0}


def test_hold_gives_joint_state_number_with_base_and_lift(monkeypatch, tmp_path):
    import json

    f = tmp_path / "post.json"
    f.write_text(json.dumps(_payload(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("RULESET_POSTERIOR_FILE", str(f))
    out = hold_confidence.evaluate("hold", ROW, _RULES)
    assert out["hold_confidence_pct"] == 34.03
    text = out["hold_confidence_text"]
    assert "持有可信度 34.0%" in text and "全体基准 34.4%" in text
    assert "lift 0.99" in text and "无区分度" in text      # lift≈1 必须直说没区分度
    assert "N=40,106" in text


def test_sample_too_small_refuses_to_print_a_number(monkeypatch, tmp_path):
    import json

    joint = dict(_payload()["joint_hold"], enough=False)
    f = tmp_path / "post.json"
    f.write_text(json.dumps(_payload(hold_confidence_pct=None, joint_hold=joint,
                                     hold_confidence_note="样本不足（N=12），不给数字"),
                            ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("RULESET_POSTERIOR_FILE", str(f))
    out = hold_confidence.evaluate("hold", ROW, _RULES)
    assert out["hold_confidence_pct"] is None
    assert "样本不足" in out["hold_confidence_text"]


def test_replay_for_a_different_rule_set_gives_nothing(monkeypatch, tmp_path):
    import json

    f = tmp_path / "post.json"
    f.write_text(json.dumps(_payload(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("RULESET_POSTERIOR_FILE", str(f))
    other = dict(_RULES, sell_rules=[{"label": "别的", "expr": "rsi14 > 50"}])
    out = hold_confidence.evaluate("hold", ROW, other)
    assert out["hold_confidence_pct"] is None and "另一套" in out["hold_confidence_text"]
    # 没有回放文件时也要说一句清楚的话，而不是整行空白
    monkeypatch.setenv("RULESET_POSTERIOR_FILE", str(tmp_path / "none.json"))
    assert "还没跑过" in hold_confidence.evaluate("hold", ROW, _RULES)["hold_confidence_text"]


def test_sell_prints_the_fired_rule_not_the_joint_hold_state(monkeypatch, tmp_path):
    import json

    f = tmp_path / "post.json"
    f.write_text(json.dumps(_payload(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("RULESET_POSTERIOR_FILE", str(f))
    out = hold_confidence.evaluate("sell", ROW, _RULES)
    assert out["hold_confidence_pct"] is None        # 「全部不触发」此刻不成立
    text = out["hold_confidence_text"]
    assert "跌破10日线5%" in text and "兑现(跌超3%) 74.2%" in text
    assert "卖飞(涨过3%) 35.0%" in text and "lift 1.23" in text
    assert out["hold_confidence_rule"]["hit_pct"] == 74.2


def test_add_prints_paired_excess_vs_same_day_hold(monkeypatch, tmp_path):
    import json

    f = tmp_path / "post.json"
    f.write_text(json.dumps(_payload(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("RULESET_POSTERIOR_FILE", str(f))
    out = hold_confidence.evaluate("add", dict(ROW, close=10.40, rsi14=20.0), _RULES)
    assert "加仓规则「趋势」" in out["hold_confidence_text"]
    assert "-0.41pp" in out["hold_confidence_text"]      # 比同日「未触发」还差，得说出来


def test_sell_from_disaster_line_says_so(monkeypatch, tmp_path):
    import json

    f = tmp_path / "post.json"
    f.write_text(json.dumps(_payload(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("RULESET_POSTERIOR_FILE", str(f))
    calm = dict(ROW, close=10.40, rsi14=20.0)          # 没有一条形态规则在响
    out = hold_confidence.evaluate("sell", calm, _RULES)
    assert "成本灾难线" in out["hold_confidence_text"]
