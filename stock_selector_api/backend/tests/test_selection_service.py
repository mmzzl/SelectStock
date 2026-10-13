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
             "price": "11.20", "score": 0.62, "rank": "1",
             "extra": {"risk_ex10_pp": "-0.21", "buy_ref_price": "10.85",
                       "buy_invalid_price": "10.31",
                       "risk_verdict": "adjust",
                       "risk_reason": "[position] 单票上限 4 万元，建议改为 3800 股",
                       "risk_shares": "3800", "risk_amount": "39140.0",
                       "risk_fill_price": "10.31", "risk_slippage_bp": "12.5",
                       "signal_posterior": {"verdict": "换时代/换抽样会翻号，未标定",
                                            "excess_pp": -0.32, "n": 166120}}},
            {"code": "", "signal": "box_oscillate"},      # 无代码脏行必须丢掉
        ]})


async def test_http_run_maps_enriched_fields(monkeypatch):
    monkeypatch.setattr(selection_service.httpx, "AsyncClient", _StubClient)
    rows = await selection_service._http_run(["trend_rebound"])
    assert len(rows) == 1
    row = rows[0]
    assert (row["strategy"], row["asof"]) == ("trend_rebound", "2026-09-24")
    assert row["price"] == 11.2 and row["score"] == 0.62 and row["rank_no"] == 1
    # 风险分藏在 extra 里，且可能是字符串；它是"过闸门的票才有"的证据分
    assert row["risk_ex10_pp"] == -0.21
    # 买点参考位/失效位同样来自 extra
    assert row["buy_ref_price"] == 10.85 and row["buy_invalid_price"] == 10.31
    # 风控决策：App 要显示「买多少股、预估什么价成交、为什么改」，字段全部来自内核
    assert (row["risk_verdict"], row["risk_shares"]) == ("adjust", 3800)
    assert row["risk_amount"] == 39140.0 and row["risk_fill_price"] == 10.31
    assert row["risk_slippage_bp"] == 12.5
    assert row["risk_reason"].startswith("[position]")
    # 判据后验藏在 extra.signal_posterior：verdict 是页面唯一该显示的字段（它已含翻号/样本不足），
    # 数字另存两列，供排序与「未标定」判定用。
    assert row["posterior_verdict"] == "换时代/换抽样会翻号，未标定"
    assert row["posterior_excess_pp"] == -0.32 and row["posterior_n"] == 166120


async def test_http_run_maps_posterior_absent_as_unlabeled(monkeypatch):
    """后验表还没建时内核不挂这个 key → 必须落空串/NULL，不能落 0（0 是「超额为零」这个判定）。"""
    class _NoPosteriorClient(_StubClient):
        async def post(self, url, json=None):
            return _StubResponse({"code": 0, "data": [{
                "code": "000003", "signal": "box_oscillate",
                "extra": {"risk_ex10_pp": "0.10", "signal_posterior": None}}]})

    monkeypatch.setattr(selection_service.httpx, "AsyncClient", _NoPosteriorClient)
    row = (await selection_service._http_run([]))[0]
    assert row["posterior_verdict"] == ""
    assert row["posterior_excess_pp"] is None and row["posterior_n"] is None
    # 风险分照常映射，说明后验缺失不会连带打掉别的字段
    assert row["risk_ex10_pp"] == 0.10


async def test_http_run_tolerates_missing_enriched_fields(monkeypatch):
    """老插件/别的实现不给富化字段时，落库必须是 None 而不是把整个任务打挂。"""
    class _BareClient(_StubClient):
        async def post(self, url, json=None):
            return _StubResponse({"code": 0, "data": [{"code": "000002", "signal": "box_oscillate"}]})

    monkeypatch.setattr(selection_service.httpx, "AsyncClient", _BareClient)
    row = (await selection_service._http_run([]))[0]
    assert row["price"] is None and row["score"] is None and row["rank_no"] is None
    # 没有 extra 时风险分必须是 None，不能落成 0（0 表示"预测跑平全市场"，是评价不是缺省）
    assert row["risk_ex10_pp"] is None
    assert row["buy_ref_price"] is None and row["buy_invalid_price"] is None
    # 风控关闭时跑的批次：verdict 是空串（没判过），下单量必须是 None 而不是 0（0 = 建议买 0 股）
    assert row["risk_verdict"] == "" and row["risk_reason"] == ""
    assert row["risk_shares"] is None and row["risk_amount"] is None
    assert row["risk_fill_price"] is None and row["risk_slippage_bp"] is None
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
