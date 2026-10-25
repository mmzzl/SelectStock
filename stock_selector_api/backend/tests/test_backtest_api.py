"""回测与系统监控接口：透传链路 + 权限点。"""
import sys
import uuid

import pytest

from services import backtest_service as svc


class _Sent:
    def __init__(self, task_id):
        self.id = task_id


@pytest.fixture()
def fake_celery(monkeypatch):
    monkeypatch.setattr(svc.celery_app, "send_task",
                        lambda name, args=None, **kw: _Sent("celery-" + uuid.uuid4().hex[:6]))
    monkeypatch.setattr(svc, "_read_celery", lambda tid: ("STARTED", None))
    monkeypatch.setattr(svc, "_ping_workers", lambda: ["bt@localhost"])


async def test_options_lists_strategies_and_defaults(client, admin_headers, fake_celery):
    r = await client.get("/api/v1/backtest/options", headers=admin_headers)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert "trend_rebound" in data["strategies"]
    assert data["defaults"]["capital"] == 100000.0
    assert data["numeric"]["max_positions"]["max"] == 50
    # 列策略名靠扫目录，不允许把内核依赖 import 进 API 进程
    assert "backtest_main" not in sys.modules


async def test_run_then_list_and_detail(client, admin_headers, fake_celery):
    body = {"params": {"engine": "self", "start": "2026-01-01", "end": "2026-02-01",
                       "capital": 50000, "target_amount": 5000}}
    r = await client.post("/api/v1/backtest/run", json=body, headers=admin_headers)
    assert r.status_code == 200, r.text
    job_id = r.json()["data"]["job_id"]

    r = await client.get("/api/v1/backtest/runs", headers=admin_headers)
    items = r.json()["data"]["items"]
    assert job_id in [i["job_id"] for i in items]

    r = await client.get(f"/api/v1/backtest/runs/{job_id}", headers=admin_headers)
    assert r.json()["data"]["status"] == "running"

    r = await client.get("/api/v1/backtest/workers", headers=admin_headers)
    assert r.json()["data"]["online"] is True


async def test_run_rejects_unknown_param(client, admin_headers, fake_celery):
    r = await client.post("/api/v1/backtest/run", json={"params": {"oops": 1}}, headers=admin_headers)
    assert r.status_code == 400, r.text
    assert "未知回测参数" in r.json()["message"]


async def test_run_requires_permission(client, fake_celery):
    """未登录时统一 401，权限依赖不会把任务投出去。"""
    r = await client.post("/api/v1/backtest/run", json={"params": {}})
    assert r.status_code == 401


async def test_system_metrics(client, admin_headers):
    r = await client.get("/api/v1/system/metrics", headers=admin_headers)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert 0 <= data["cpu"]["percent"] <= 100
    assert data["memory"]["total"] > 0 and 0 <= data["memory"]["percent"] <= 100
    assert data["disks"], "至少要采到一个真实分区"
    assert {"path", "total", "used", "percent"} <= set(data["disks"][0])
