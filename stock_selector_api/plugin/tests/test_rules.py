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

def test_disaster_stop_pct_default(monkeypatch):
    monkeypatch.setenv("PLUGIN_RULES_FILE", "")
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
