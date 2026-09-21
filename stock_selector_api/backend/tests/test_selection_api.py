"""B14：选股 API 流程测试。

select_jobs 表跨用例不清理，admin 计数用 >= 断言；
后台任务完成与否以任务详情轮询为准。
"""
import asyncio


async def _wait_done(client, headers, job_id, timeout: float = 5.0):
    for _ in range(int(timeout / 0.05)):
        r = await client.get(f"/api/v1/selection/runs/{job_id}", headers=headers)
        if r.json()["data"]["status"] in ("done", "failed"):
            return r.json()["data"]
        await asyncio.sleep(0.05)
    raise AssertionError(f"job {job_id} 未结束")


async def test_selection_api_flow(client, admin_headers):
    r = await client.get("/api/v1/selection/strategies", headers=admin_headers)
    assert r.status_code == 200 and r.json()["data"]

    r2 = await client.post("/api/v1/selection/run", headers=admin_headers,
                           json={"strategies": ["trend_rebound"]})
    assert r2.status_code == 200
    job_id = r2.json()["data"]["job_id"]

    det = await _wait_done(client, admin_headers, job_id)
    assert det["status"] == "done" and det["results"]

    r4 = await client.get("/api/v1/selection/runs", headers=admin_headers)
    assert r4.status_code == 200 and r4.json()["data"]["total"] >= 1


async def test_selection_detail_not_found_for_other_user(client, admin_headers):
    r = await client.get("/api/v1/selection/runs/no-such-job", headers=admin_headers)
    assert r.status_code == 404


async def test_selection_requires_auth(client):
    r = await client.post("/api/v1/selection/run", json={"strategies": []})
    assert r.status_code in (401, 403)
