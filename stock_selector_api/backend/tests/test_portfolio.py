"""M1 红线契约测试：持仓诊断 analyze 契约（幂等重放 / 用户隔离 / 判定收敛透传）。

重构：删除自建 app_fx（settings.database_url 在 conftest 中被置 None 会崩），
改用 conftest 的 client fixture（复用 session 级内存 SQLite，见 backend/tests/conftest.py）。
每个用例后清空诊断双表，保证计数断言不受跨用例污染。
"""

import pytest
from sqlalchemy import text

import db.base as dbb

ANALYZE_BODY = {
    "user_id": "user_p0_aho9",
    "natural_day": "2026-09-15",
    "portfolio_snapshot_md5": "md5_abc123",
    "holdings": [
        {"code": "600519", "name": "贵州茅台", "quantity": 100, "cost_price": 1688.0},
        {"code": "000858", "name": "五粮液", "quantity": 50, "cost_price": 142.0},
    ],
}


@pytest.fixture(autouse=True)
async def _clean_diag():
    yield
    async with dbb._engine.begin() as conn:
        await conn.execute(text("DELETE FROM portfolio_diag_results"))
        await conn.execute(text("DELETE FROM portfolio_diag_runs"))


async def _db_runs():
    async with dbb._engine.begin() as conn:
        rows = await conn.execute(text(
            "SELECT run_id, user_id, natural_day, status FROM portfolio_diag_runs"
        ))
        return rows.fetchall()


class TestAnalyzeContract:
    async def test_r2i_idempotent_replay_reuses_run_id(self, client):
        # 同 user_id+natural_day+快照 → 两次调用返回同一 run_id，且只建 1 条 run
        r1 = await client.post("/api/portfolio/analyze", json=ANALYZE_BODY)
        assert r1.status_code == 200, r1.text
        run1 = r1.json()["data"]["run_id"]

        r2 = await client.post("/api/portfolio/analyze", json=ANALYZE_BODY)
        assert r2.status_code == 200, r2.text
        run2 = r2.json()["data"]["run_id"]

        assert run1 == run2, "幂等重放必须复用既有 run_id"
        runs = await _db_runs()
        assert len(runs) == 1, f"幂等重放不得重复建 run，实际 {len(runs)} 条"
        assert runs[0].status == "completed"

    async def test_user_id_isolation_no_cross_user(self, client):
        # 用户 A 的 run 对用户 B 不可见（list 接口按 user_id 隔离）
        r = await client.post(
            "/api/portfolio/analyze",
            json={**ANALYZE_BODY, "user_id": "user_p0_aho9_owner"},
        )
        assert r.status_code == 200, r.text

        lr = await client.get("/api/portfolio/runs", params={"user_id": ANALYZE_BODY["user_id"]})
        assert lr.status_code == 200, lr.text
        data = lr.json()["data"]
        assert all(x["user_id"] == ANALYZE_BODY["user_id"] for x in data.get("items", [])), data

    async def test_backend_never_computes_judgment(self, client):
        # 判定零后端计算：结果每只都带插件 action 字段（sell/hold/add）与收益快照
        r = await client.post("/api/portfolio/analyze", json=ANALYZE_BODY)
        assert r.status_code == 200, r.text
        body = r.json()["data"]
        results = body.get("results", [])
        assert len(results) == 2, "每只持仓都要有判定结果"
        for item in results:
            assert item["action"] in {"sell", "hold", "add"}, item
            assert "latest_price" in item and "holding_pnl" in item