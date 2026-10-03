"""M1 红线契约测试：持仓诊断 analyze（幂等重放 / 用户隔离 / 判定收敛透传）。

走 service 而非 HTTP：路由的属主已由登录令牌决定（见 test_diag_api.py），
这里只锁插件代理与批次键语义。
每个用例后清空诊断双表，保证计数断言不受跨用例污染。
"""

import pytest
from sqlalchemy import text

import db.base as dbb
from services import portfolio_service

HOLDINGS = [
    {"code": "600519", "name": "贵州茅台", "quantity": 100, "cost_price": 1688.0},
    {"code": "000858", "name": "五粮液", "quantity": 50, "cost_price": 142.0},
]
USER_A = "user_p0_aho9"
USER_B = "user_p0_aho9_owner"
DAY = "2026-09-15"


@pytest.fixture(autouse=True)
async def _clean_diag():
    async with dbb._engine.begin() as conn:
        await conn.execute(text("DELETE FROM portfolio_diag_results"))
        await conn.execute(text("DELETE FROM portfolio_diag_runs"))
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
    async def test_r2i_idempotent_replay_reuses_run_id(self):
        # 同 user_id+natural_day+快照 → 两次调用返回同一 run_id，且只建 1 条 run
        out1 = await portfolio_service.analyze(USER_A, DAY, HOLDINGS)
        out2 = await portfolio_service.analyze(USER_A, DAY, HOLDINGS)
        assert out1["run_id"] == out2["run_id"], "幂等重放必须复用既有 run_id"
        runs = await _db_runs()
        assert len(runs) == 1, f"幂等重放不得重复建 run，实际 {len(runs)} 条"
        assert runs[0].status == "completed"

    async def test_user_id_isolation_no_cross_user(self):
        # 属主隔离：不同 user_id 同快照 → 各自一条 run
        out_a = await portfolio_service.analyze(USER_A, DAY, HOLDINGS)
        out_b = await portfolio_service.analyze(USER_B, DAY, HOLDINGS)
        assert out_a["run_id"] != out_b["run_id"]
        items, _ = await portfolio_service.list_runs(USER_A, 1, 20)
        assert {i["run_id"] for i in items} == {out_a["run_id"]}

    async def test_backend_never_computes_judgment(self):
        # 判定零后端计算：结果每只都带插件 action 字段（sell/hold/add）与收益快照
        body = await portfolio_service.analyze(USER_A, DAY, HOLDINGS)
        results = body["results"]
        assert len(results) == 2, "每只持仓都要有判定结果"
        for item in results:
            assert item["action"] in {"sell", "hold", "add"}, item
            assert "latest_price" in item and "holding_pnl" in item
