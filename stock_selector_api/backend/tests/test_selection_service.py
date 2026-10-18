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
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

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
                       "adx14_pct": "97.53", "adx14_gate": "pass",
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
    # adx14 闸门留痕同样藏在 extra 里，且分位是字符串（内核给的是 JSON 数值）
    assert row["adx14_pct"] == 97.53 and row["adx14_gate"] == "pass"


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


async def test_http_run_maps_gate_absent_as_unlabeled(monkeypatch):
    """闸门没开（或分位取不到）时内核不挂这两个 key → 落 NULL/空串。

    特别不能把 reject 落成 `adx14_pct=0`：0 分位是"当日全市场最弱"这个事实，
    而 reject 可能只是分位 83 没到 90 门槛——两者混起来，shadow 台账的对照就废了。
    """
    class _NoGateClient(_StubClient):
        async def post(self, url, json=None):
            return _StubResponse({"code": 0, "data": [{
                "code": "000004", "signal": "box_oscillate",
                "extra": {"risk_verdict": "allow"}}]})

    monkeypatch.setattr(selection_service.httpx, "AsyncClient", _NoGateClient)
    row = (await selection_service._http_run([]))[0]
    assert row["adx14_pct"] is None and row["adx14_gate"] == ""
    assert row["risk_verdict"] == "allow"          # 别的字段照常映射


async def test_http_run_keeps_engine_na_declaration(monkeypatch):
    """引擎显式声明「这一项我不算」的清单必须原样落库。

    那些数值列本身空着分不出两种成因（引擎压根不算 / 算了但这只判不出），
    而 App 显示的是「—」还是「不适用」完全靠这份清单——把它丢了，qlib 名单上的
    「预期 10 日超额」空白就会被读成「预测跑平大盘」，等于把没测说成没事。
    """
    class _QlibClient(_StubClient):
        async def post(self, url, json=None):
            return _StubResponse({"code": 0, "data": [{
                "code": "600000", "signal": "预期5日收益排名", "strategy": "qlib_alpha158",
                "extra": {"na_fields": ["risk_ex10_pp", "buy_ref_price",
                                        "buy_invalid_price", "posterior_verdict"]}}]})

    monkeypatch.setattr(selection_service.httpx, "AsyncClient", _QlibClient)
    row = (await selection_service._http_run([]))[0]
    assert row["na_fields"] == "risk_ex10_pp,buy_ref_price,buy_invalid_price,posterior_verdict"
    assert row["risk_ex10_pp"] is None and row["buy_ref_price"] is None

    # 清单不是列表（老插件给字符串/None）→ 必须是空串：
    # 「没声明」和「声明了全部不适用」在页面上是两种完全不同的话。
    class _BadClient(_StubClient):
        async def post(self, url, json=None):
            return _StubResponse({"code": 0, "data": [{
                "code": "600001", "extra": {"na_fields": "risk_ex10_pp"}}]})

    monkeypatch.setattr(selection_service.httpx, "AsyncClient", _BadClient)
    assert (await selection_service._http_run([]))[0]["na_fields"] == ""


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
    assert row["na_fields"] == ""                   # 没声明 ≠ 声明了不适用


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


# ── 僵尸任务：单飞位不能被一个已经死掉的作业永久锁死 ────────────────────────────
# 2026-10-13 实测踩到：手动验证时进程在 `_run_job` 真正跑起来之前退出，那条作业永远停在
# queued，于是 App 上每点一次「开始选股」都是「已有选股任务运行中」，而那条任务早就不存在了。

def _async_return(value):
    """造一个「await 一下就返回 value」的协程函数，给 monkeypatch 用。"""
    async def _f(*a, **k):
        return value
    return _f


def _job_row(status="queued", minutes_old=0):
    from datetime import datetime, timedelta, timezone
    when = datetime.now(timezone.utc) - timedelta(minutes=minutes_old)
    return {"job_id": "j-stale", "status": status, "created_at": when.isoformat(),
            "updated_at": when.isoformat(), "error": ""}


async def test_start_run_blocks_on_a_live_job(monkeypatch):
    monkeypatch.setattr(selection_service, "_inflight", {})
    monkeypatch.setattr(selection_service.select_jobs_repo, "find_running",
                        _async_return(_job_row(minutes_old=1)))
    with pytest.raises(BizError, match="已有选股任务运行中"):
        await selection_service.start_run("u1", ["pattern_reversal"])


async def test_start_run_releases_stale_job_instead_of_blocking(monkeypatch):
    """超过 30 分钟没进展的 in-flight 作业先判失败，再放行新任务。"""
    monkeypatch.setattr(selection_service, "_inflight", {})
    monkeypatch.setattr(selection_service.select_jobs_repo, "find_running",
                        _async_return(_job_row(minutes_old=90)))
    patched: dict = {}

    async def _update(job_id, patch):
        patched.update(patch)
        return {}

    monkeypatch.setattr(selection_service.select_jobs_repo, "update", _update)
    created: dict = {}

    async def _create(row):
        created.update(row)
        return {"job_id": "j-new"}

    monkeypatch.setattr(selection_service.select_jobs_repo, "create", _create)
    monkeypatch.setattr(selection_service.asyncio, "create_task", lambda *a, **k: None)
    out = await selection_service.start_run("u1", ["pattern_reversal"])
    assert out["job_id"] == "j-new"
    assert patched["status"] == "failed" and "已自动释放" in patched["error"]
    assert created["status"] == "queued"          # 僵尸已清，新任务正常建


async def test_stale_release_handles_unparsable_timestamp(monkeypatch):
    """时间戳坏掉时按"很旧"处理：宁可多跑一次扫描，也不能把功能锁死。"""
    monkeypatch.setattr(selection_service, "_inflight", {})
    bad = {"job_id": "j-bad", "status": "running", "created_at": "not-a-date",
           "updated_at": None, "error": ""}
    monkeypatch.setattr(selection_service.select_jobs_repo, "find_running", _async_return(bad))
    seen: dict = {}

    async def _update(job_id, patch):
        seen.update(patch)
        return {}

    monkeypatch.setattr(selection_service.select_jobs_repo, "update", _update)
    assert await selection_service._release_stale("u1") is None
    assert seen["status"] == "failed"


def test_age_minutes_treats_naive_timestamps_as_utc():
    """老库里的时间戳没有 tzinfo（SQLite 存的 UTC）；按本地时区算会让"僵尸"看起来是未来。"""
    from datetime import datetime, timezone
    naive = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    job = {"created_at": naive.isoformat(), "updated_at": naive.isoformat()}
    assert -1 <= selection_service._age_minutes(job) <= 1
    assert selection_service._age_minutes({}) == float("inf")


# ── 持仓的「当日榜内名次」：薄代理，后端不排名、不补名次 ──────────────────────────

class _RankClient:
    """替掉 httpx.AsyncClient 的 GET。记录请求参数，好验「代码原样传给引擎」这条。"""

    seen: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None):
        type(self).seen = {"url": url, "params": params}
        return _StubResponse({"code": 0, "data": {
            "date": "2026-10-19", "pool_size": 300, "universe": 299, "items": [
                {"code": "600000", "in_pool": True, "rank": "7", "universe": 299,
                 "score_raw": 0.0312},
                # 池外的持仓：引擎给的就是 rank=null + 一句原因，代理不许给它编一个数
                {"code": "510300", "in_pool": False, "rank": None, "universe": 299,
                 "note": "不在当前 300 只候选池里，模型没给这只打过分"},
            ]}})


async def test_day_ranks_passes_through_without_inventing(monkeypatch):
    monkeypatch.setattr(selection_service.settings, "selection_plugin_mode", "http")
    monkeypatch.setattr(selection_service.httpx, "AsyncClient", _RankClient)
    out = await selection_service.day_ranks(["600000", "510300"])
    assert _RankClient.seen["params"] == {"codes": "600000,510300"}
    assert _RankClient.seen["url"].endswith("/api/selector/rank")
    assert (out["supported"], out["date"], out["pool_size"], out["universe"]) == (
        True, "2026-10-19", 300, 299)
    # 卖出线和回测 band 用的是同一个缺省值：页面说的"跌出榜外"必须和回测的卖点是同一条线
    assert out["exit_rank"] == selection_service.BACKTEST_DEFAULTS["exit_rank"]
    by_code = {it["code"]: it for it in out["items"]}
    assert by_code["600000"]["rank"] == 7            # 字符串名次收敛成整数
    assert by_code["510300"]["rank"] is None         # 池外必须是 None，不是 0/301/999
    assert by_code["510300"]["in_pool"] is False
    assert "候选池" in by_code["510300"]["note"]


async def test_day_ranks_404_is_explicitly_unsupported(monkeypatch):
    """自研内核（8002）没有这条路由：必须说「这个引擎不提供名次」，不能退化成空名单。

    空名单在前端和「持仓全都没上榜」长得一样，那会把"没这个功能"说成"你的票全跌出去了"，
    而后者是会让人卖票的结论。
    """
    class _Missing(_RankClient):
        async def get(self, url, params=None):
            return _StubResponse({"detail": "Not Found"}, status_code=404)

    monkeypatch.setattr(selection_service.settings, "selection_plugin_mode", "http")
    monkeypatch.setattr(selection_service.httpx, "AsyncClient", _Missing)
    out = await selection_service.day_ranks(["600000"])
    assert out["supported"] is False and out["items"] == []
    assert "不提供榜内名次" in out["message"]


async def test_day_ranks_error_raises_instead_of_empty_board(monkeypatch):
    """引擎 5xx 时报业务错，让页面显示查询失败——返回空 items 会被读成「都没上榜」。"""
    class _Broken(_RankClient):
        async def get(self, url, params=None):
            return _StubResponse({"detail": "qlib 名次查询失败"}, status_code=502)

    monkeypatch.setattr(selection_service.settings, "selection_plugin_mode", "http")
    monkeypatch.setattr(selection_service.httpx, "AsyncClient", _Broken)
    with pytest.raises(BizError):
        await selection_service.day_ranks(["600000"])


async def test_day_ranks_without_plugin_is_unsupported(monkeypatch):
    """mock 模式没有真引擎，名次这一项就是没有——supported=False 而不是造两行假名次。"""
    monkeypatch.setattr(selection_service.settings, "selection_plugin_mode", "mock")
    out = await selection_service.day_ranks(["600000"])
    assert out["supported"] is False and out["items"] == []
    assert out["exit_rank"] == selection_service.BACKTEST_DEFAULTS["exit_rank"]


def test_split_codes_rules():
    """代码串的口径：认市场前缀、去重保序、认不出就报错、超 60 只报错。

    超限必须是错误而不是截断：插件那侧是 [:60] 静默截断，截断后的持仓页少显示几只票的
    名次，看起来像「那几只没上榜」，而实际是根本没去查。
    """
    assert selection_service.split_codes("600000, SH510300 ,600000,") == ["600000", "510300"]
    assert selection_service.split_codes("600519.SH") == ["600519"]
    assert selection_service.split_codes("") == []
    with pytest.raises(BizError):
        selection_service.split_codes("600000,平安银行")
    with pytest.raises(BizError):
        selection_service.split_codes(",".join(f"60{i:04d}" for i in range(61)))
