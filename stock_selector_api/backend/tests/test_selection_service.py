import asyncio
import uuid

import pytest

from core.exceptions import BizError
from repositories import inbox_repo, select_jobs_repo, select_results_repo
from services import selection_service


async def _wait_job(job_id: str, timeout: float = 5.0) -> dict:
    """后台任务落库轮询（测试事件循环内 create_task 与断言并发）。"""
    for _ in range(int(timeout / 0.02)):
        job = await select_jobs_repo.get(job_id)
        if job and job["status"] in ("done", "failed"):
            return job
        await asyncio.sleep(0.02)
    raise AssertionError(f"job {job_id} 未在超时内结束")


async def _wait_inbox(user_id: str, timeout: float = 5.0) -> str | None:
    """notify 在状态更新之后写 inbox，同样需轮询等待。"""
    for _ in range(int(timeout / 0.02)):
        mid = await inbox_repo.latest_id(user_id)
        if mid:
            return mid
        await asyncio.sleep(0.02)
    return None


def _mk_user():
    return f"us-{uuid.uuid4().hex[:8]}"


async def test_manual_run_completes_with_results():
    uid = _mk_user()
    out = await selection_service.start_run(uid, ["trend_rebound"])
    assert out["job_id"] and out["status"] in ("queued", "done")
    job = await _wait_job(out["job_id"])
    assert job["status"] == "done" and job["result_count"] > 0
    assert await select_results_repo.list_by_job(out["job_id"])
    last = await _wait_inbox(uid)
    assert last is not None       # 完成即发站内信
    assert (await inbox_repo.get(last))["event_code"] == "selection_completed"


async def test_single_flight_rejects_duplicate():
    uid = _mk_user()
    await selection_service.start_run(uid, ["trend_rebound"])
    with pytest.raises(BizError):
        await selection_service.start_run(uid, ["trend_rebound"])


async def test_detail_and_ownership():
    uid = _mk_user()
    out = await selection_service.start_run(uid, ["trend_rebound"])
    await _wait_job(out["job_id"])
    det = await selection_service.run_status(uid, out["job_id"])
    assert det["results"]
    with pytest.raises(BizError):
        await selection_service.run_status("other-user", out["job_id"])


async def test_list_runs_paged():
    uid = _mk_user()
    out = await selection_service.start_run(uid, None)
    await _wait_job(out["job_id"])
    page = await selection_service.list_runs(uid, 1, 20)
    assert page["total"] >= 1
    assert page["items"][0]["result_count_now"] == 2


async def test_strategies_mock():
    assert set(await selection_service.list_strategies()) >= {"trend_rebound", "box_oscillate"}


class _StubResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _StubClient:
    """替掉 httpx.AsyncClient：只验字段映射，不发真实请求。"""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        return _StubResponse({"code": 0, "data": [
            {"code": "000001", "name": "平安银行", "signal": "trend_rebound", "reason": "回踩 MA20",
             "strategy": "trend_rebound", "asof": "2026-09-24",
             "price": "11.20", "score": 0.62, "rank": "1", "extra": {}},
            {"code": "", "signal": "box_oscillate"},      # 无代码脏行必须丢掉
        ]})


async def test_http_run_maps_enriched_fields(monkeypatch):
    monkeypatch.setattr(selection_service.httpx, "AsyncClient", _StubClient)
    rows = await selection_service._http_run(["trend_rebound"])
    assert len(rows) == 1
    row = rows[0]
    assert (row["strategy"], row["asof"]) == ("trend_rebound", "2026-09-24")
    assert row["price"] == 11.2 and row["score"] == 0.62 and row["rank_no"] == 1


async def test_http_run_tolerates_missing_enriched_fields(monkeypatch):
    """老插件/别的实现不给富化字段时，落库必须是 None 而不是把整个任务打挂。"""
    class _BareClient(_StubClient):
        async def post(self, url, json=None):
            return _StubResponse({"code": 0, "data": [{"code": "000002", "signal": "box_oscillate"}]})

    monkeypatch.setattr(selection_service.httpx, "AsyncClient", _BareClient)
    row = (await selection_service._http_run([]))[0]
    assert row["price"] is None and row["score"] is None and row["rank_no"] is None
    assert row["strategy"] == "" and row["asof"] == ""


async def test_mock_run_failure_notifies(monkeypatch):
    async def boom(strategies):
        raise RuntimeError("boom")

    monkeypatch.setattr(selection_service, "_mock_run", boom)
    uid = _mk_user()
    out = await selection_service.start_run(uid, ["trend_rebound"])
    job = await _wait_job(out["job_id"])
    assert job["status"] == "failed" and "boom" in job["error"]
    mid = await _wait_inbox(uid)
    assert mid is not None
    assert (await inbox_repo.get(mid))["event_code"] == "selection_failed"
