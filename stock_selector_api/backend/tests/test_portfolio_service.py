"""持仓诊断服务：幂等重放 / 用户隔离 / 判定收敛透传（零后端算术）。"""

from repositories import portfolio_results, portfolio_runs
from services import portfolio_service

HOLDINGS = [
    {"code": "600519", "name": "贵州茅台", "quantity": 100, "cost_price": 1688.0},
    {"code": "000858", "name": "五粮液", "quantity": 50, "cost_price": 142.0},
]


async def _clean():
    await portfolio_results._delete_from("portfolio_diag_results", {})
    await portfolio_runs._delete_from("portfolio_diag_runs", {})


async def test_analyze_create_and_return_results():
    await _clean()
    out = await portfolio_service.analyze("svc-u1", "2026-09-16", HOLDINGS)
    assert out["run_id"]
    assert out["status"] == "completed"
    assert len(out["results"]) == 2
    for r in out["results"]:
        assert r["action"] in {"sell", "hold", "add"}
        assert "latest_price" in r and "holding_pnl" in r
    await _clean()


async def test_analyze_idempotent_replay():
    await _clean()
    a = await portfolio_service.analyze("svc-u2", "2026-09-16", HOLDINGS)
    b = await portfolio_service.analyze("svc-u2", "2026-09-16", HOLDINGS)
    assert a["run_id"] == b["run_id"]
    runs = await portfolio_runs._find_all({"user_id": "svc-u2"})
    assert len(runs) == 1
    assert runs[0]["status"] == "completed"
    await _clean()


async def test_analyze_user_isolation_key():
    await _clean()
    a = await portfolio_service.analyze("svc-u3", "2026-09-16", HOLDINGS)
    b = await portfolio_service.analyze("svc-u3b", "2026-09-16", HOLDINGS)
    assert a["run_id"] != b["run_id"]
    await _clean()


async def test_list_runs_user_isolation():
    await _clean()
    await portfolio_service.analyze("svc-u4", "2026-09-16", HOLDINGS)
    items, total = await portfolio_service.list_runs("svc-u4")
    assert total >= 1 and all(x["user_id"] == "svc-u4" for x in items)
    items, total = await portfolio_service.list_runs("svc-other")
    assert total == 0
    await _clean()


async def test_sell_trigger_price_roundtrip(monkeypatch):
    """插件反算的卖出价格线必须一路走到接口出参：漏字段会静默变成"没有卖点价"。"""
    await _clean()

    async def fake_plugin(payload):
        return {"results": [{
            "code": "600519", "name": "贵州茅台", "action": "hold", "action_reason": "x",
            "latest_price": 1688.0, "quantity": 100, "holding_pnl": 0.0, "pnl_pct": 0.0,
            "suggestion": "", "sell_trigger_price": 1603.6,
        }], "summary": {"action_counts": {"hold": 1}, "total_market_value": 168800.0,
                        "total_pnl": 0.0, "holding_count": 1}}

    monkeypatch.setattr("services.portfolio_plugin.call_plugin_analyze", fake_plugin)
    out = await portfolio_service.analyze("svc-u5", "2026-09-16", HOLDINGS[:1])
    assert out["results"][0]["sell_trigger_price"] == 1603.6
    row = (await portfolio_results.list_by_run(out["run_id"]))[0]
    assert row["sell_trigger_price"] == 1603.6

    # 幂等重放走库里读回来的那条路径，同样要带得上
    again = await portfolio_service.analyze("svc-u5", "2026-09-16", HOLDINGS[:1])
    assert again["results"][0]["sell_trigger_price"] == 1603.6
    await _clean()