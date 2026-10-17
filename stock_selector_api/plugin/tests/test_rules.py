"""test_rules.py"""
import json
from pathlib import Path

from portfolio.rules import load_rules


def test_defaults_when_file_empty(tmp_path, monkeypatch):
    cfg = tmp_path / "portfolio_rules.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("PLUGIN_RULES_FILE", str(cfg))
    rules = load_rules()
    assert "sell" in rules and "add" in rules and "hold" in rules
    assert isinstance(rules["sell"], str) and len(rules["sell"]) > 0


def test_json_overrides_defaults(tmp_path, monkeypatch):
    cfg = tmp_path / "portfolio_rules.json"
    cfg.write_text(json.dumps({"sell": "close < 1", "add": "close > 2"}),
                   encoding="utf-8")
    monkeypatch.setenv("PLUGIN_RULES_FILE", str(cfg))
    rules = load_rules()
    assert rules["sell"] == "close < 1"
    assert rules["add"] == "close > 2"


def test_file_absent_falls_back_to_defaults(monkeypatch):
    monkeypatch.setenv("PLUGIN_RULES_FILE", "/nonexistent/x.json")
    rules = load_rules()
    assert rules["sell"] and rules["add"] and rules["hold"]

def test_disaster_stop_pct_default():
    assert load_rules()["disaster_stop_pct"] == 8.0


def test_disaster_stop_pct_override_and_clamp(tmp_path, monkeypatch):
    cfg = tmp_path / "portfolio_rules.json"
    monkeypatch.setenv("PLUGIN_RULES_FILE", str(cfg))
    cfg.write_text(json.dumps({"disaster_stop_pct": 12}), encoding="utf-8")
    assert load_rules()["disaster_stop_pct"] == 12.0
    cfg.write_text(json.dumps({"disaster_stop_pct": 0.1}), encoding="utf-8")
    assert load_rules()["disaster_stop_pct"] == 1.0
    cfg.write_text(json.dumps({"disaster_stop_pct": "abc"}), encoding="utf-8")
    assert load_rules()["disaster_stop_pct"] == 8.0


def test_empty_rule_lists_turn_that_side_off(tmp_path, monkeypatch):
    """显式给空列表 = 这一侧一条形态规则都没有（2026-10-17 停掉未标定占位规则用的就是这形状）。

    关键在「列表给了但是是空的」和「根本没给这个键」必须是两种意思：后者继续用默认常量，
    否则关不掉，而判定会拿一条从没标定过的趋势线去建议卖出。
    """
    from portfolio.rules import DEFAULT_DISASTER_STOP_PCT

    cfg = tmp_path / "active.json"
    cfg.write_text(json.dumps({"sell_rules": [], "add_rules": [],
                               "disaster_stop_pct": DEFAULT_DISASTER_STOP_PCT}), encoding="utf-8")
    monkeypatch.setenv("PLUGIN_RULES_FILE", str(cfg))
    rules = load_rules()
    assert rules["sell"] == "" and rules["add"] == ""
    assert rules["sell_rules"] == [] and rules["add_rules"] == []
    assert rules["disaster_stop_pct"] == 8.0          # 灾难线是单独参数，关形态规则不动它


def test_missing_or_null_keys_keep_defaults(tmp_path, monkeypatch):
    from portfolio.rules import DEFAULT_ADD_RULE, DEFAULT_SELL_RULE

    cfg = tmp_path / "active.json"
    monkeypatch.setenv("PLUGIN_RULES_FILE", str(cfg))
    cfg.write_text(json.dumps({"sell_rules": None}), encoding="utf-8")
    rules = load_rules()
    assert rules["sell"] == DEFAULT_SELL_RULE and rules["add"] == DEFAULT_ADD_RULE
