"""选股服务：薄代理选股插件(8002) + job 生命周期 + 完成/失败通知。
红线：不在后端做任何选股逻辑；只有代理与落库。"""
from __future__ import annotations

import asyncio
import json
import logging

import httpx

from config import settings
from core.exceptions import BizError, NotFoundError
from repositories import select_jobs_repo, select_results_repo
from services import inbox_notify

logger = logging.getLogger("selection_service")

# 进程内单飞表：DB 状态查询与后台任务间存在调度间隙，内存判定保证确定性
_inflight: dict[str, str] = {}


async def list_strategies() -> list[str]:
    if settings.selection_plugin_mode == "http":
        url = settings.selection_plugin_base_url.rstrip("/") + "/api/strategy/list"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            body = resp.json()
        return list(body.get("data") or [])
    return ["trend_rebound", "box_oscillate"]


# 插件未启时的兜底元信息，字段形状与 GET /api/strategy/detail 保持一致
_MOCK_STRATEGY_DETAILS = [
    {"name": "trend_rebound", "description": "趋势回踩：均线多头排列，股价回踩均线企稳", "as_of": True,
     "required_columns": ["date", "open", "high", "low", "close", "volume"], "required_rows": 30,
     "params": {"period": 20, "pullback_pct": 0.05}},
    {"name": "box_oscillate", "description": "箱体震荡：区间突破上沿买入", "as_of": True,
     "required_columns": ["date", "open", "high", "low", "close", "volume"], "required_rows": 40,
     "params": {"period": 30, "breakout_pct": 0.02}},
]


async def list_strategy_details() -> list[dict]:
    """策略元信息（中文说明/取数需求），前端拿它做中文标签，避免在客户端硬编码策略名。"""
    if settings.selection_plugin_mode == "http":
        url = settings.selection_plugin_base_url.rstrip("/") + "/api/strategy/detail"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            body = resp.json()
        return list(body.get("data") or [])
    return _MOCK_STRATEGY_DETAILS


async def _mock_run(strategies: list[str]) -> list[dict]:
    """确定性模拟结果（测试/插件未启时）。"""
    return [
        {"code": "600519", "name": "贵州茅台", "signal": "趋势回踩", "reason": "mock: MA20 上穿",
         "strategy": "trend_rebound", "asof": "2026-09-24", "price": 1480.0, "score": 0.82,
         "rank_no": 1},
        {"code": "000858", "name": "五粮液", "signal": "箱体突破", "reason": "mock: 突破箱顶",
         "strategy": "box_oscillate", "asof": "2026-09-24", "price": 128.5, "score": 1.0,
         "rank_no": 2},
    ]


def _num(value, integer: bool = False):
    """插件给的可能是 str/None，落库前收敛成数值或 None（rank 走整数口径）。"""
    try:
        return None if value in (None, "") else (int(float(value)) if integer else float(value))
    except (TypeError, ValueError):
        return None


async def _http_run(strategies: list[str]) -> list[dict]:
    url = settings.selection_plugin_base_url.rstrip("/") + "/api/selector/run"
    async with httpx.AsyncClient(timeout=600) as client:
        resp = await client.post(url, json={"strategies": strategies or None})
        resp.raise_for_status()
        body = resp.json()
    items = body.get("data") or []
    return [{
        "code": str(it.get("code", "")),
        "name": str(it.get("name", "")),
        "signal": str(it.get("signal", "") or it.get("action", "")),
        "reason": str(it.get("reason", "")),
        "strategy": str(it.get("strategy", "")),
        "asof": str(it.get("asof", "")),
        "price": _num(it.get("price")),
        "score": _num(it.get("score")),
        "rank_no": _num(it.get("rank"), integer=True),
        # 买入风险分在插件的扩展字段里；闸门关闭或历史太短判分时没有这个 key → None，
        # 不能落成 0（0 是"预测超额为零"，是有含义的评价）
        "risk_ex10_pp": _num((it.get("extra") or {}).get("risk_ex10_pp")),
        # 买点参考位/失效位：策略判据里本来就算出来的均线、箱沿、6 日低点，
        # 没有对应判据的策略给 None（不能落成 0，也不能从 reason 文本里抠）
        "buy_ref_price": _num((it.get("extra") or {}).get("buy_ref_price")),
        "buy_invalid_price": _num((it.get("extra") or {}).get("buy_invalid_price")),
        # 风控决策：信号已过五条规则才到这里（被拦的票内核就不返回了）。
        # allow=原样放行、adjust=放行但下单量被单票上限压过；空值=这批结果跑在风控关闭时。
        "risk_verdict": str((it.get("extra") or {}).get("risk_verdict") or "")[:8],
        "risk_reason": str((it.get("extra") or {}).get("risk_reason") or "")[:512],
        "risk_shares": _num((it.get("extra") or {}).get("risk_shares"), integer=True),
        "risk_amount": _num((it.get("extra") or {}).get("risk_amount")),
        "risk_fill_price": _num((it.get("extra") or {}).get("risk_fill_price")),
        "risk_slippage_bp": _num((it.get("extra") or {}).get("risk_slippage_bp")),
    } for it in items if it.get("code")]


async def start_run(user_id: str, strategies: list[str] | None,
                    source: str = "manual") -> dict:
    if not user_id:
        raise BizError("user_id 不能为空", code=400, status_code=400)
    if user_id in _inflight:
        raise BizError(f"已有选股任务运行中: {_inflight[user_id]}", code=409, status_code=409)
    running = await select_jobs_repo.find_running(user_id)
    if running:
        raise BizError(f"已有选股任务运行中: {running['job_id']}", code=409, status_code=409)
    strategies = strategies or ["trend_rebound", "box_oscillate"]
    job = await select_jobs_repo.create({
        "user_id": user_id,
        "strategies": json.dumps(strategies, ensure_ascii=False),
        "status": "queued",
        "source": source,
    })
    _inflight[user_id] = job["job_id"]
    asyncio.create_task(_run_job(job["job_id"], user_id, strategies))
    return {"job_id": job["job_id"], "status": "queued"}


async def _run_job(job_id: str, user_id: str, strategies: list[str]) -> None:
    try:
        await select_jobs_repo.update(job_id, {"status": "running"})
        if settings.selection_plugin_mode == "http":
            results = await _http_run(strategies)
        else:
            results = await _mock_run(strategies)
        for r in results:
            await select_results_repo.create({"job_id": job_id, **r})
        await select_jobs_repo.update(job_id, {"status": "done", "result_count": len(results)})
        await inbox_notify.notify_user(
            user_id, "selection_completed", "选股完成",
            f"本次选股命中 {len(results)} 只（策略: {'/'.join(strategies)}）",
            related_type="select_jobs", related_id=job_id)
    except Exception as exc:    # noqa: BLE001
        logger.exception("选股任务失败: %s", job_id)
        await select_jobs_repo.update(job_id, {"status": "failed", "error": str(exc)[:512]})
        try:
            await inbox_notify.notify_user(
                user_id, "selection_failed", "选股失败", str(exc)[:512],
                related_type="select_jobs", related_id=job_id)
        except Exception:       # noqa: BLE001
            logger.exception("选股失败通知发送失败: %s", job_id)
    finally:
        if _inflight.get(user_id) == job_id:
            _inflight.pop(user_id, None)


async def run_status(user_id: str, job_id: str) -> dict:
    job = await select_jobs_repo.get(job_id)
    if not job or job["user_id"] != user_id:
        raise NotFoundError("选股任务不存在")
    job["results"] = await select_results_repo.list_by_job(job_id)
    return job


async def list_runs(user_id: str, page: int = 1, size: int = 20) -> dict:
    items, total = await select_jobs_repo.list_by_user(user_id, page, size)
    for job in items:
        rows = await select_results_repo.list_by_job(job["job_id"])
        job["result_count_now"] = len(rows)
    return {"items": items, "total": total}
