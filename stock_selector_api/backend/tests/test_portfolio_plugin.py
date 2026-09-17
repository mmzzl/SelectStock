"""插件客户端（mock 模式）：判定+收益快照结构、确定性。M2 前仅占位。"""

from services import portfolio_plugin

HOLDINGS = [
    {"code": "600519", "name": "贵州茅台", "quantity": 100, "cost_price": 1688.0},
    {"code": "000858", "name": "五粮液", "quantity": 50, "cost_price": 142.0},
    {"code": "300750", "name": "宁德时代", "quantity": 200, "cost_price": 180.0},
]


async def test_mock_plugin_returns_results_and_summary():
    out = await portfolio_plugin.call_plugin_analyze({"user_id": "u1", "holdings": HOLDINGS})
    assert len(out["results"]) == 3
    for r, h in zip(out["results"], HOLDINGS):
        assert r["code"] == h["code"]
        assert r["action"] in portfolio_plugin.ACTIONS
        assert r["latest_price"] > 0
        assert "holding_pnl" in r and "pnl_pct" in r
    s = out["summary"]
    assert s["holding_count"] == 3
    assert sum(s["action_counts"].values()) == 3


async def test_mock_plugin_replay_deterministic():
    a = await portfolio_plugin.call_plugin_analyze({"holdings": HOLDINGS[:2]})
    b = await portfolio_plugin.call_plugin_analyze({"holdings": HOLDINGS[:2]})
    assert a == b