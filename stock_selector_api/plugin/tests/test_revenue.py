"""test_revenue.py"""
from portfolio.revenue import snapshot, summarize


def test_snapshot_positive_pnl():
    r = snapshot({"code": "600519", "name": "茅台", "quantity": 100, "cost_price": 100.0}, 110.0)
    assert r["latest_price"] == 110.0
    assert r["holding_pnl"] == 1000.0
    assert r["pnl_pct"] == 10.0
    assert r["action_reason"] == ""


def test_snapshot_zero_cost_pnl_pct_zero():
    r = snapshot({"code": "000001", "name": "平安", "quantity": 10, "cost_price": 0.0}, 5.0)
    assert r["holding_pnl"] == 50.0
    assert r["pnl_pct"] == 0.0


def test_summarize():
    results = [
        {"action": "sell", "latest_price": 10.0, "quantity": 100, "holding_pnl": 50.0},
        {"action": "hold", "latest_price": 20.0, "quantity": 10, "holding_pnl": 0.0},
    ]
    s = summarize(results)
    assert s["action_counts"] == {"sell": 1, "hold": 1}
    assert s["total_market_value"] == 1200.0
    assert s["total_pnl"] == 50.0
    assert s["holding_count"] == 2
