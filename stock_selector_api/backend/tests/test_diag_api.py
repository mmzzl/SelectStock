"""tests/test_diag_api.py — M3 Task6：外部调度契约 + 诊断明细端点。"""

import asyncio

import pytest
from repositories import holdings_repo
from services import diag_service


@pytest.fixture(autouse=True)
async def _seed():
    await diag_service.reset_runtime()
    await holdings_repo._delete_from("holdings", {})
    await holdings_repo.create({"user_id": "u_api", "code": "600519", "quantity": 100,
                                "cost_price": 100})
    yield
    for r in await holdings_repo.list_by_user("u_api"):
        await holdings_repo.delete(r["id"])
    await holdings_repo._delete_from("holdings", {})


async def test_external_diag_endpoint(client, monkeypatch):
    async def fake_plugin(payload):
        return {"results": [
            {"code": h["code"], "name": "", "action": "hold", "action_reason": "",
             "latest_price": 10.0, "quantity": h["quantity"], "holding_pnl": 0.0,
             "pnl_pct": 0.0, "suggestion": ""} for h in payload["holdings"]],
            "summary": {"action_counts": {"hold": 1}, "total_market_value": 1000.0,
                        "total_pnl": 0.0, "holding_count": 1}}

    monkeypatch.setattr("services.portfolio_plugin.call_plugin_analyze", fake_plugin)
    r = await client.post("/api/external/portfolio/diag",
                          json={"user_id": "u_api", "natural_day": "2026-09-17"})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    data = body["data"]
    assert data["run_id"] and data["status"] in ("queued", "running", "completed")
    for _ in range(50):
        detail = await diag_service.diag_detail(data["run_id"])
        if detail and detail["status"] in ("completed", "failed"):
            break
        await asyncio.sleep(0.02)
    assert detail["status"] == "completed"


async def test_diag_detail_endpoint(client, monkeypatch):
    async def fake_plugin(payload):
        return {"results": [], "summary": {"action_counts": {}, "total_market_value": 0,
                                           "total_pnl": 0, "holding_count": 0}}

    monkeypatch.setattr("services.portfolio_plugin.call_plugin_analyze", fake_plugin)
    r = await client.post("/api/external/portfolio/diag",
                          json={"user_id": "u_api", "natural_day": "2026-09-16"})
    run_id = r.json()["data"]["run_id"]
    for _ in range(50):
        d = await diag_service.diag_detail(run_id)
        if d and d["status"] in ("completed", "failed"):
            break
        await asyncio.sleep(0.02)
    r2 = await client.get(f"/api/portfolio/diag/{run_id}")
    assert r2.status_code == 200
    assert r2.json()["data"]["run_id"] == run_id