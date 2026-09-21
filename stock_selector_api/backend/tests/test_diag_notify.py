"""B15：诊断完成 → 站内信通知。"""
import asyncio
import uuid

from repositories import holdings_repo, inbox_repo, portfolio_runs
from services import diag_service


async def test_diag_completion_notifies():
    uid = f"u-{uuid.uuid4().hex[:8]}"
    await holdings_repo.create({"user_id": uid, "code": "600519", "name": "茅台",
                                "quantity": 100, "cost_price": 1688.0})
    out = await diag_service.trigger_diag(uid, "2026-09-20")
    assert out and out["status"] in ("queued", "running", "completed")

    msg_id = None
    for _ in range(100):
        msg_id = await inbox_repo.latest_id(uid)
        if msg_id:
            break
        await asyncio.sleep(0.05)
    assert msg_id is not None
    msg = await inbox_repo.get(msg_id)
    assert msg["event_code"] == "portfolio_diag_completed"
    assert msg["related_type"] == "portfolio_diag_runs" and msg["related_id"] == out["run_id"]

    run = await portfolio_runs.get(out["run_id"])
    assert run["status"] == "completed"
