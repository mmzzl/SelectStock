"""tests/test_diag_service.py — M3 Task5：外部诊断批次（幂等批次键 / 单飞 / 重试3次 / failed）。"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from core.exceptions import BizError
from repositories import holdings_repo, portfolio_results, portfolio_runs
from services import diag_service


@pytest.fixture(autouse=True)
async def _seed():
    await diag_service.reset_runtime()
    await portfolio_runs._delete_from("portfolio_diag_runs", {})
    await portfolio_results._delete_from("portfolio_diag_results", {})
    await holdings_repo._delete_from("holdings", {})
    await holdings_repo.create({"user_id": "u1", "code": "600519", "quantity": 100,
                                "cost_price": 100})
    await holdings_repo.create({"user_id": "u1", "code": "000858", "quantity": 50,
                                "cost_price": 50})
    yield
    await portfolio_runs._delete_from("portfolio_diag_runs", {})
    await portfolio_results._delete_from("portfolio_diag_results", {})
    await holdings_repo._delete_from("holdings", {})


async def test_first_diag_creates_run_and_completes(monkeypatch):
    calls = []

    async def fake_plugin(payload):
        calls.append(payload["user_id"])
        return {"results": [
            {"code": p["code"], "name": p.get("name", ""), "action": "hold",
             "action_reason": "x", "latest_price": 10.0, "quantity": p["quantity"],
             "holding_pnl": 0.0, "pnl_pct": 0.0, "suggestion": ""}
            for p in payload["holdings"]],
            "summary": {"action_counts": {"hold": 2},
                        "total_market_value": 1000.0, "total_pnl": 0.0,
                        "holding_count": 2}}

    monkeypatch.setattr("services.portfolio_plugin.call_plugin_analyze", fake_plugin)
    out = await diag_service.trigger_diag("u1", "2026-09-17")
    assert out["status"] in ("queued", "running", "completed")
    for _ in range(100):
        run = await portfolio_runs.get(out["run_id"])
        if run["status"] in ("completed", "failed"):
            break
        await asyncio.sleep(0.02)
    assert run["status"] == "completed"
    results = await portfolio_results.list_by_run(out["run_id"])
    assert len(results) == 2


async def test_idempotent_replay_same_run_id(monkeypatch):
    count = {"n": 0}

    async def fake_plugin(payload):
        count["n"] += 1
        return {"results": [], "summary": {"action_counts": {}, "total_market_value": 0,
                                           "total_pnl": 0, "holding_count": 0}}

    monkeypatch.setattr("services.portfolio_plugin.call_plugin_analyze", fake_plugin)
    out1 = await diag_service.trigger_diag("u1", "2026-09-16")
    key = ("u1", "2026-09-16")
    for _ in range(200):
        r = await portfolio_runs.get(out1["run_id"])
        if r and r["status"] == "completed" and key not in diag_service._RUNNING:
            break
        await asyncio.sleep(0.02)
    out2 = await diag_service.trigger_diag("u1", "2026-09-16")
    assert out1["run_id"] == out2["run_id"]
    assert count["n"] == 1


async def _fake_results(payload):
    return {"results": [
        {"code": p["code"], "name": p.get("name", ""), "action": "hold",
         "action_reason": "x", "latest_price": 10.0, "quantity": p["quantity"],
         "holding_pnl": 0.0, "pnl_pct": 0.0, "suggestion": ""}
        for p in payload["holdings"]],
        "summary": {"action_counts": {"hold": 2}, "total_market_value": 1000.0,
                    "total_pnl": 0.0, "holding_count": 2}}


async def test_completed_batch_replays_inside_cooldown_then_reruns_after(monkeypatch):
    """刷新入口的冷却规则：10 分钟内点「立即诊断」只重放旧结果，过了冷却才原地重跑刷新行情。"""
    count = {"n": 0}

    async def fake_plugin(payload):
        count["n"] += 1
        return await _fake_results(payload)

    monkeypatch.setattr("services.portfolio_plugin.call_plugin_analyze", fake_plugin)
    out1 = await diag_service.trigger_diag("u1", "2026-09-11")
    key = ("u1", "2026-09-11")
    for _ in range(200):
        r = await portfolio_runs.get(out1["run_id"])
        if r["status"] in ("completed", "failed") and key not in diag_service._RUNNING:
            break
        await asyncio.sleep(0.02)
    assert r["status"] == "completed"

    out2 = await diag_service.trigger_diag("u1", "2026-09-11")
    assert out2["run_id"] == out1["run_id"]
    assert count["n"] == 1, "冷却期内不该重算"

    # 把落定时间推回 11 分钟，模拟「行情已经旧了」
    await portfolio_runs._update_where_one(
        "portfolio_diag_runs", {"run_id": out1["run_id"]},
        {"updated_at": datetime.now(timezone.utc) - timedelta(minutes=11)})
    out3 = await diag_service.trigger_diag("u1", "2026-09-11")
    assert out3["run_id"] == out1["run_id"] and out3["status"] == "queued"
    for _ in range(200):
        r3 = await portfolio_runs.get(out3["run_id"])
        if r3["status"] in ("completed", "failed") and key not in diag_service._RUNNING:
            break
        await asyncio.sleep(0.02)
    assert r3["status"] == "completed"
    assert count["n"] == 2
    results = await portfolio_results.list_by_run(out1["run_id"])
    assert len(results) == 2, "原地重跑要重建明细而不是翻倍"


def test_minutes_since_treats_naive_timestamp_as_utc():
    """updated_at 是 UTC naive 入库的：按本机（UTC+8）本地时间做减法会凭空多算 8 小时。"""
    ts = datetime.now(timezone.utc) - timedelta(minutes=30)
    assert diag_service._minutes_since({"updated_at": ts}) == pytest.approx(30, abs=1)
    assert diag_service._minutes_since({"updated_at": ts.replace(tzinfo=None)}) == pytest.approx(30, abs=1)
    assert diag_service._minutes_since({"updated_at": "2026-09-11 10:00"}) is None
    assert diag_service._minutes_since({}) is None


async def test_retry_3_times_then_failed(monkeypatch):
    attempts = {"n": 0}

    async def failing(payload):
        attempts["n"] += 1
        raise RuntimeError("plugin down")

    monkeypatch.setattr("services.portfolio_plugin.call_plugin_analyze", failing)
    monkeypatch.setattr(diag_service, "_BACKOFF_BASE", 0.0)
    out = await diag_service.trigger_diag("u1", "2026-09-15")
    for _ in range(200):
        run = await portfolio_runs.get(out["run_id"])
        if run["status"] in ("completed", "failed"):
            break
        await asyncio.sleep(0.02)
    assert run["status"] == "failed"
    assert attempts["n"] == 3
    assert run["error"]


async def test_no_holdings_no_run(monkeypatch):
    for r in await holdings_repo.list_by_user("u1"):
        await holdings_repo.delete(r["id"])
    assert await diag_service.trigger_diag("u1", "2026-09-14") is None


async def test_single_flight_rejects_concurrent(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(payload):
        started.set()
        await release.wait()
        return {"results": [], "summary": {"action_counts": {}, "total_market_value": 0,
                                           "total_pnl": 0, "holding_count": 0}}

    monkeypatch.setattr("services.portfolio_plugin.call_plugin_analyze", slow)
    t1 = asyncio.create_task(diag_service.trigger_diag("u1", "2026-09-13"))
    await started.wait()
    with pytest.raises(BizError):
        await diag_service.trigger_diag("u1", "2026-09-13")
    release.set()
    first = await t1
    for _ in range(100):
        r = await portfolio_runs.get(first["run_id"])
        if r["status"] in ("completed", "failed"):
            break
        await asyncio.sleep(0.02)
    assert r["status"] == "completed"

async def test_failed_batch_reruns_on_next_trigger(monkeypatch):
    """failed 批次不是终点：再次触发原地重跑，且半截明细被清掉不重复。"""
    async def failing(payload):
        raise RuntimeError("plugin down")

    monkeypatch.setattr("services.portfolio_plugin.call_plugin_analyze", failing)
    monkeypatch.setattr(diag_service, "_BACKOFF_BASE", 0.0)
    out1 = await diag_service.trigger_diag("u1", "2026-09-12")
    key = ("u1", "2026-09-12")
    for _ in range(200):
        r = await portfolio_runs.get(out1["run_id"])
        # 状态先落 failed、_RUNNING 才清除：只看状态就再触发会撞 409（真机连点两次同理）
        if r["status"] == "failed" and key not in diag_service._RUNNING:
            break
        await asyncio.sleep(0.02)
    assert r["status"] == "failed"

    await portfolio_results.create({"run_id": out1["run_id"], "code": "600519",
                                    "name": "", "action": "hold", "action_reason": "x",
                                    "latest_price": 1.0, "quantity": 1, "holding_pnl": 0.0,
                                    "pnl_pct": 0.0, "suggestion": ""})

    calls = {"n": 0}

    async def ok_plugin(payload):
        calls["n"] += 1
        return {"results": [
            {"code": p["code"], "name": p.get("name", ""), "action": "hold",
             "action_reason": "x", "latest_price": 10.0, "quantity": p["quantity"],
             "holding_pnl": 0.0, "pnl_pct": 0.0, "suggestion": ""}
            for p in payload["holdings"]],
            "summary": {"action_counts": {"hold": 2}, "total_market_value": 1000.0,
                        "total_pnl": 0.0, "holding_count": 2}}

    monkeypatch.setattr("services.portfolio_plugin.call_plugin_analyze", ok_plugin)
    out2 = await diag_service.trigger_diag("u1", "2026-09-12")
    assert out2["run_id"] == out1["run_id"]
    for _ in range(200):
        r2 = await portfolio_runs.get(out2["run_id"])
        if r2["status"] in ("completed", "failed") and key not in diag_service._RUNNING:
            break
        await asyncio.sleep(0.02)
    assert r2["status"] == "completed"
    assert not r2["error"]
    assert calls["n"] == 1
    results = await portfolio_results.list_by_run(out2["run_id"])
    assert len(results) == 2
