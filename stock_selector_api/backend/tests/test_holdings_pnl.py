import uuid

import pytest
from sqlalchemy import text

import db.base as dbb
from repositories import holdings_repo, portfolio_results, portfolio_runs
from services import holdings_service


@pytest.fixture(autouse=True)
async def _clean_diag():
    """本文件会种诊断 run/result；用后清空，避免污染 test_portfolio 的全局计数断言。"""
    yield
    async with dbb._engine.begin() as conn:
        await conn.execute(text("DELETE FROM portfolio_diag_results"))
        await conn.execute(text("DELETE FROM portfolio_diag_runs"))


async def test_pnl_proxies_to_plugin():
    uid = f"u-{uuid.uuid4().hex[:8]}"
    await holdings_repo.create({"user_id": uid, "code": "600519", "name": "茅台",
                                "quantity": 100, "cost_price": 1604.0})
    out = await holdings_service.pnl(uid, "2026-09-20")
    assert "per_code" in out and "realized_summary" in out
    assert out["per_code"][0]["avg_cost"] == 1604.0
    # 未实现盈亏来自插件（mock 以成本为基准）：断言结构而非精确值，禁后端算术
    assert out["unrealized_summary"]["holding_count"] == 1


async def test_pnl_empty_no_holdings():
    out = await holdings_service.pnl("ghost-user", "2026-09-20")
    assert out["per_code"] == []
    assert out["realized_summary"] == {"total_realized": 0.0, "total_fees": 0.0}
    assert out["unrealized_summary"]["holding_count"] == 0


async def test_list_holdings_enriched_with_diag():
    uid = f"u-{uuid.uuid4().hex[:8]}"
    await holdings_repo.create({"user_id": uid, "code": "600519", "name": "茅台",
                                "quantity": 100, "cost_price": 1688.0})
    run = await portfolio_runs.create({"batch_id": "seed", "user_id": uid,
                                       "natural_day": "2026-09-20", "status": "completed",
                                       "held_code_count": 1, "action_count": 1})
    await portfolio_results.create({"run_id": run["run_id"], "code": "600519", "name": "茅台",
                                    "action": "sell", "action_reason": "跌破MA10",
                                    "latest_price": 1500.0, "quantity": 100,
                                    "holding_pnl": -18800.0, "pnl_pct": -11.14})
    rows = await holdings_service.list_holdings(uid)
    assert rows[0]["code"] == "600519"
    assert rows[0]["action"] == "sell"
    assert rows[0]["latest_price"] == 1500.0
    assert rows[0]["holding_pnl"] == -18800.0
    assert rows[0]["diag_run_id"] == run["run_id"]


async def test_list_holdings_no_diag_leaves_rows_clean():
    uid = f"u-{uuid.uuid4().hex[:8]}"
    await holdings_repo.create({"user_id": uid, "code": "000001", "name": "平安",
                                "quantity": 1000, "cost_price": 10.0})
    rows = await holdings_service.list_holdings(uid)
    assert rows[0]["code"] == "000001"
    assert "diag_run_id" not in rows[0]
