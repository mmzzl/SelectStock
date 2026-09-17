"""持仓诊断 Repository：幂等键查找 + 明细按 run 关联。"""

from repositories import portfolio_results, portfolio_runs


async def _clean() -> None:
    await portfolio_results._delete_from("portfolio_diag_results", {})
    await portfolio_runs._delete_from("portfolio_diag_runs", {})


async def test_run_create_and_find_by_key():
    await _clean()
    run = await portfolio_runs.create({
        "batch_id": "md5-k1", "user_id": "repo-u1", "natural_day": "2026-09-16",
        "status": "queued", "held_code_count": 1,
    })
    assert run["run_id"]
    found = await portfolio_runs.find_by_key("repo-u1", "2026-09-16", "md5-k1")
    assert found and found["run_id"] == run["run_id"]
    assert await portfolio_runs.find_by_key("repo-u1", "2026-09-16", "md5-other") is None
    await _clean()


async def test_result_write_and_list_by_run():
    await _clean()
    run = await portfolio_runs.create({
        "batch_id": "md5-k2", "user_id": "repo-u2", "natural_day": "2026-09-16",
        "status": "completed", "held_code_count": 2,
    })
    await portfolio_results.create({
        "run_id": run["run_id"], "code": "600519", "action": "hold",
        "latest_price": 1700.0, "quantity": 100, "holding_pnl": 200.0, "pnl_pct": 1.2,
    })
    rows = await portfolio_results.list_by_run(run["run_id"])
    assert len(rows) == 1 and rows[0]["code"] == "600519"
    assert rows[0]["action"] == "hold"
    await _clean()