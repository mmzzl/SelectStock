"""test_analyzer.py"""
import pytest

from portfolio.analyzer import analyze_holdings
from portfolio.rules import load_rules


class FakeProvider:
    """确定性行情/指标：每只持仓一行最新指标。"""

    def __init__(self, rows: dict):
        self.rows = rows

    def latest_row(self, code: str) -> dict | None:
        return self.rows.get(code)


HOLDINGS = [
    {"code": "600001", "name": "甲", "quantity": 100, "cost_price": 10.0},
    {"code": "600002", "name": "乙", "quantity": 50, "cost_price": 20.0},
    {"code": "600003", "name": "丙", "quantity": 10, "cost_price": 30.0},
]

ROWS = {
    "600001": {"close": 9.0, "price": 9.0, "ma10": 10.0, "ma5": 10.0, "ma20": 10.0,
               "rsi14": 40.0, "vol_ma5": 100.0, "volume": 100.0},   # sell（9<=9.5）
    "600002": {"close": 25.0, "price": 25.0, "ma10": 24.0, "ma5": 20.0, "ma20": 20.0,
               "rsi14": 50.0, "vol_ma5": 100.0, "volume": 100.0},   # add
    "600003": {"close": 30.0, "price": 30.0, "ma10": 29.0, "ma5": 30.0, "ma20": 29.0,
               "rsi14": 40.0, "vol_ma5": 100.0, "volume": 100.0},   # hold
}


def test_analyze_judges_and_computes_revenue(monkeypatch):
    monkeypatch.setenv("PLUGIN_RULES_FILE", "")   # 用默认规则
    out = analyze_holdings(HOLDINGS, FakeProvider(ROWS))
    by_code = {r["code"]: r for r in out["results"]}
    assert by_code["600001"]["action"] == "sell"
    assert by_code["600002"]["action"] == "add"
    assert by_code["600003"]["action"] == "hold"
    # 收益快照由插件算（R1-4）
    assert by_code["600001"]["latest_price"] == 9.0
    assert by_code["600001"]["holding_pnl"] == round((9.0 - 10.0) * 100, 2)
    assert by_code["600002"]["holding_pnl"] == round((25.0 - 20.0) * 50, 2)
    # summary
    s = out["summary"]
    assert s["action_counts"] == {"sell": 1, "add": 1, "hold": 1}
    assert s["total_market_value"] == round(9.0 * 100 + 25.0 * 50 + 30.0 * 10, 2)
    assert s["holding_count"] == 3


def test_analyze_missing_row_sorted_to_sell(monkeypatch):
    monkeypatch.setenv("PLUGIN_RULES_FILE", "")
    rows = dict(ROWS)
    del rows["600003"]                       # 行情缺失
    out = analyze_holdings(HOLDINGS, FakeProvider(rows))
    by_code = {r["code"]: r for r in out["results"]}
    assert by_code["600003"]["action"] == "hold"
    assert by_code["600003"]["action_reason"] == "行情缺失，维持原判不做交易建议"
