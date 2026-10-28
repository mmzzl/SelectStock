"""灾难线只有一把尺子：线上判定和回测审计不许各抄一份常量。

2026-10-28 的分叉就是这么来的：用户点名 −5%，我只改了回测，`data/rules/active.json` 还留着 8.0，
浮亏 7.8% 的票照旧判 hold。这两个审计脚本原本也各自写着 −0.08，所以这里锁的是「读同一份规则」。
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "stock_selector_api" / "plugin"))

from portfolio.rules import load_rules  # noqa: E402


def _load_script(name):
    spec = importlib.util.spec_from_file_location(name, f"scripts/{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_audit_sell_rules_disaster_tail_follows_the_live_line():
    mod = _load_script("audit_sell_rules")
    expected = -load_rules()["disaster_stop_pct"] / 100.0
    assert mod.TAIL_LINES[0] == expected


def test_ruleset_posterior_disaster_tail_follows_the_live_line():
    mod = _load_script("ruleset_posterior")
    expected = -load_rules()["disaster_stop_pct"] / 100.0
    assert mod.TAIL8 == expected
