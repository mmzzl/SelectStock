"""回测服务：投递 Celery + 状态归并。用假的 send_task/_read_celery，测试不碰 Redis。"""
import asyncio
import json
import uuid

import pytest

from core.backtest_params import DEFAULTS
from core.exceptions import BizError, NotFoundError
from repositories import backtest_jobs_repo, inbox_repo
from services import backtest_service as svc


class _Sent:
    def __init__(self, task_id):
        self.id = task_id


def _user():
    return "bt-" + uuid.uuid4().hex[:8]


def _params():
    # 这些键属于自研内核（target_amount/universe 是 qlib 不实现的），引擎必须点名 self
    return {"engine": "self", "start": "2026-01-01", "end": "2026-02-01",
            "capital": 50000, "target_amount": 5000}


def _summary():
    return {
        "task_id": "kernel-1", "strategy": "kernel:trend_rebound",
        "start_date": "2026-01-01", "end_date": "2026-02-01",
        "metrics": {"total_return": -0.03, "trade_count": 4},
        "equity": [{"date": "2026-01-01", "total_value": 49900.0}],
        "recent_trades": [{"code": "000001", "name": "平安银行", "side": "sell",
                           "date": "2026-01-20", "price": 11.2, "shares": 300,
                           "amount": 3360.0, "fee": 6.7, "pnl": -30.0}],
        "trade_count": 4,
    }


@pytest.fixture()
def sent(monkeypatch):
    calls = []

    def fake_send_task(name, args=None, **kwargs):
        calls.append({"name": name, "args": args})
        return _Sent("celery-" + uuid.uuid4().hex[:6])

    monkeypatch.setattr(svc.celery_app, "send_task", fake_send_task)
    return calls


async def _wait_inbox(user_id):
    for _ in range(40):
        mid = await inbox_repo.latest_id(user_id)
        if mid:
            return mid
        await asyncio.sleep(0.05)
    return None


async def test_start_run_sends_task_and_records_job(sent):
    uid = _user()
    out = await svc.start_run(uid, _params())
    job = await backtest_jobs_repo.get(out["job_id"])
    assert job["status"] == "queued"
    assert job["celery_task_id"].startswith("celery-")
    assert json.loads(job["params"])["capital"] == 50000.0
    assert sent[0]["name"] == svc.settings.backtest_task_name
    # 投递给 worker 的只能是白名单内的参数
    assert set(sent[0]["args"][0]) <= set(DEFAULTS)


async def test_single_flight_rejects_second_run(sent):
    uid = _user()
    await svc.start_run(uid, _params())
    with pytest.raises(BizError):
        await svc.start_run(uid, _params())


async def test_invalid_params_rejected_before_send(sent):
    with pytest.raises(BizError):
        await svc.start_run(_user(), {"start": "2026-02-01", "end": "2026-01-01"})
    assert sent == []


async def test_broker_down_marks_job_failed(monkeypatch):
    uid = _user()

    def boom(name, args=None, **kwargs):
        raise ConnectionError("redis down")

    monkeypatch.setattr(svc.celery_app, "send_task", boom)
    with pytest.raises(BizError):
        await svc.start_run(uid, _params())
    jobs, _ = await backtest_jobs_repo.list_by_user(uid, 1, 10)
    assert jobs[0]["status"] == "failed"
    assert "redis down" in jobs[0]["error"]


async def test_sync_progress_then_success_persists_summary(sent, monkeypatch):
    uid = _user()
    job_id = (await svc.start_run(uid, _params()))["job_id"]

    monkeypatch.setattr(svc, "_read_celery",
                        lambda tid: ("PROGRESS", {"progress": 42, "stage": "逐日撮合 8/19"}))
    detail = await svc.job_detail(uid, job_id)
    assert detail["status"] == "running" and detail["progress"] == 42
    assert detail["stage"] == "逐日撮合 8/19"

    monkeypatch.setattr(svc, "_read_celery", lambda tid: ("SUCCESS", _summary()))
    detail = await svc.job_detail(uid, job_id)
    assert detail["status"] == "done" and detail["progress"] == 100
    assert detail["result"]["task_id"] == "kernel-1"
    assert detail["result"]["recent_trades"][0]["code"] == "000001"

    mid = await _wait_inbox(uid)
    assert mid and (await inbox_repo.get(mid))["event_code"] == "backtest_completed"

    # 终态后不再查 Celery，单飞也已释放
    monkeypatch.setattr(svc, "_read_celery", lambda tid: (_ for _ in ()).throw(RuntimeError("不应再查")))
    assert (await svc.job_detail(uid, job_id))["status"] == "done"
    await svc.start_run(uid, _params())


async def test_sync_failure_stores_readable_error(sent, monkeypatch):
    uid = _user()
    job_id = (await svc.start_run(uid, _params()))["job_id"]
    monkeypatch.setattr(svc, "_read_celery", lambda tid: ("FAILURE", {
        "exc_type": "RuntimeError", "exc_message": ["缺少 buy/sell 规则，无法回测"]}))
    detail = await svc.job_detail(uid, job_id)
    assert detail["status"] == "failed"
    assert detail["error"] == "RuntimeError: 缺少 buy/sell 规则，无法回测"
    mid = await _wait_inbox(uid)
    assert (await inbox_repo.get(mid))["event_code"] == "backtest_failed"


async def test_pending_job_stays_queued_until_stale(sent, monkeypatch):
    uid = _user()
    job_id = (await svc.start_run(uid, _params()))["job_id"]
    monkeypatch.setattr(svc, "_read_celery", lambda tid: ("PENDING", None))
    assert (await svc.job_detail(uid, job_id))["status"] == "queued"

    monkeypatch.setattr(svc, "_age_seconds", lambda job: svc.STALE_SECONDS + 1)
    detail = await svc.job_detail(uid, job_id)
    assert detail["status"] == "failed" and "无响应" in detail["error"]


async def test_ownership_isolated(sent):
    uid = _user()
    job_id = (await svc.start_run(uid, _params()))["job_id"]
    with pytest.raises(NotFoundError):
        await svc.job_detail("someone-else", job_id)
    assert (await svc.list_runs("someone-else", 1, 20))["total"] == 0


async def test_list_runs_summarizes_without_curve(sent, monkeypatch):
    uid = _user()
    job_id = (await svc.start_run(uid, _params()))["job_id"]
    monkeypatch.setattr(svc, "_read_celery", lambda tid: ("SUCCESS", _summary()))
    page = await svc.list_runs(uid, 1, 20)
    item = [i for i in page["items"] if i["job_id"] == job_id][0]
    assert item["result"]["metrics"]["trade_count"] == 4
    assert "equity" not in item["result"] and "recent_trades" not in item["result"]
    assert item["params"]["capital"] == 50000.0


async def test_worker_status_reports_online(sent, monkeypatch):
    monkeypatch.setattr(svc, "_ping_workers", lambda: ["bt@host2", "bt@host1"])
    st = await svc.worker_status()
    assert st["online"] and st["workers"] == ["bt@host1", "bt@host2"]

    def boom():
        raise ConnectionError("broker 不可达")

    monkeypatch.setattr(svc, "_ping_workers", boom)
    st = await svc.worker_status()
    assert not st["online"] and "broker 不可达" in st["error"]
