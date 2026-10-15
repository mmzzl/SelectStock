"""tests/test_diag_api.py — 诊断接口鉴权收敛：属主一律取自登录令牌，越权/未登录一律拒。"""

import asyncio
from datetime import date, timedelta

import pytest
from repositories import portfolio_runs
from services import diag_service

DAY_A = (date.today() - timedelta(days=3)).isoformat()
DAY_B = (date.today() - timedelta(days=4)).isoformat()


@pytest.fixture(autouse=True)
async def _reset():
    from sqlalchemy import text

    import db.base as dbb

    async def _wipe():
        async with dbb._engine.begin() as conn:
            await conn.execute(text("DELETE FROM portfolio_diag_results"))
            await conn.execute(text("DELETE FROM portfolio_diag_runs"))

    await diag_service.reset_runtime()
    await _wipe()
    yield
    await _wipe()


async def _owner_id(client, headers) -> str:
    me = await client.get("/api/v1/auth/me", headers=headers)
    return me.json()["data"]["id"]


async def _add_holding(client, headers, code="600519"):
    r = await client.post("/api/v1/holdings", headers=headers, json={
        "code": code, "name": "茅台", "quantity": 100, "cost_price": 1688.0})
    assert r.status_code == 200, r.text


async def _wait_done(run_id):
    for _ in range(100):
        detail = await diag_service.diag_detail(run_id)
        if detail and detail["status"] in ("completed", "failed"):
            return detail
        await asyncio.sleep(0.02)
    raise AssertionError(f"诊断未在预期内结束：{run_id}")


async def test_run_without_holdings_is_rejected(client, admin_headers):
    r = await client.post("/api/v1/diag/run", headers=admin_headers)
    assert r.status_code == 409
    assert "持仓" in r.json()["message"]


async def test_manual_run_completes_and_is_listed(client, admin_headers):
    await _add_holding(client, admin_headers)
    r = await client.post("/api/v1/diag/run", headers=admin_headers)
    assert r.status_code == 200, r.text
    run_id = r.json()["data"]["run_id"]

    detail = await _wait_done(run_id)
    assert detail["status"] == "completed", detail["error"]
    assert len(detail["results"]) == 1

    lr = await client.get("/api/v1/diag/runs", headers=admin_headers)
    assert lr.status_code == 200
    assert run_id in [i["run_id"] for i in lr.json()["data"]["items"]]


DIAG_EXTRA_FIELDS = ("conviction_pct", "conviction_level", "conviction_text",
                     "take_profit_price", "game_v", "game_s", "game_text")


async def test_detail_exposes_new_diagnosis_fields(client, admin_headers):
    """诊断明细出参必须带上信念/止盈/博弈 7 字段（mock 插件已同款透出）。"""
    await _add_holding(client, admin_headers)
    r = await client.post("/api/v1/diag/run", headers=admin_headers)
    assert r.status_code == 200, r.text
    run_id = r.json()["data"]["run_id"]

    detail = await _wait_done(run_id)
    assert detail["status"] == "completed", detail["error"]

    resp = await client.get(f"/api/v1/diag/runs/{run_id}", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    row = resp.json()["data"]["results"][0]
    for key in DIAG_EXTRA_FIELDS:
        assert key in row, (key, row)
        assert row[key] is not None, key


async def test_refresh_message_distinguishes_start_and_replay(client, admin_headers):
    """刷新入口的文案得说实话：真跑了一次才叫「已启动」，冷却期内重放要告诉用户多久能刷新。"""
    await _add_holding(client, admin_headers)
    r1 = await client.post("/api/v1/diag/run", headers=admin_headers)
    assert r1.json()["message"] == "诊断已启动", r1.text
    await _wait_done(r1.json()["data"]["run_id"])

    r2 = await client.post("/api/v1/diag/run", headers=admin_headers)
    assert r2.json()["data"]["run_id"] == r1.json()["data"]["run_id"]
    assert "10 分钟" in r2.json()["message"], r2.text


async def test_same_day_rerun_replays_batch(client, admin_headers):
    await _add_holding(client, admin_headers)
    r1 = await client.post("/api/external/portfolio/diag", headers=admin_headers,
                           json={"natural_day": DAY_A})
    assert r1.status_code == 200, r1.text
    run_id = r1.json()["data"]["run_id"]
    await _wait_done(run_id)

    r2 = await client.post("/api/external/portfolio/diag", headers=admin_headers,
                           json={"natural_day": DAY_A})
    assert r2.json()["data"]["run_id"] == run_id, "同日重跑必须复用批次"


async def test_run_of_other_user_not_visible(client, admin_headers):
    owner = await _owner_id(client, admin_headers)
    run = await portfolio_runs.create({"batch_id": "foreign", "user_id": owner + "-x",
                                       "natural_day": DAY_B, "status": "completed"})
    for url in (f"/api/v1/diag/runs/{run['run_id']}", f"/api/portfolio/diag/{run['run_id']}"):
        r = await client.get(url, headers=admin_headers)
        assert r.status_code == 404, (url, r.text)


@pytest.mark.parametrize("url", [
    "/api/v1/diag/runs", "/api/portfolio/runs", "/api/v1/holdings",
])
async def test_contract_routes_require_login(client, url):
    assert (await client.get(url)).status_code == 401


async def test_portfolio_runs_scoped_to_token_user(client, admin_headers):
    # 旧契约的 user_id 查询参数不再被信任：只返回令牌属主的 run
    await _add_holding(client, admin_headers)
    r = await client.post("/api/v1/diag/run", headers=admin_headers)
    run_id = r.json()["data"]["run_id"]
    await _wait_done(run_id)

    lr = await client.get("/api/portfolio/runs", headers=admin_headers,
                          params={"user_id": "someone-else"})
    items = lr.json()["data"]["items"]
    assert [i["run_id"] for i in items] == [run_id]
    assert {i["user_id"] for i in items} == {await _owner_id(client, admin_headers)}
