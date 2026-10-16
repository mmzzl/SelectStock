"""回测服务：把参数投递给 Celery worker，读回状态归并进作业表。
红线：后端不做任何回测计算，只 send_task + 查 AsyncResult；成交与净值明细由内核自己落 MongoDB。"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from celery.result import AsyncResult

from celery_app import celery_app
from config import settings
from core.backtest_params import form_spec, validate
from core.exceptions import BizError, NotFoundError
from db.sql.base import new_uuid
from repositories import backtest_jobs_repo
from services import inbox_notify, ruleset_service

logger = logging.getLogger("backtest_service")

TERMINAL = ("done", "failed")

# 排队/运行中的作业长时间拿不到任何 Celery 状态（worker 掉线、结果已过期）时判定失败，
# 否则该用户的单飞会被永久占住，页面再也发不出新任务
STALE_SECONDS = 3 * 3600

# 进程内单飞表：DB 落库与投递之间有间隙，内存判定保证重复点击的确定性
_inflight: dict[str, str] = {}


def list_strategies() -> list[str]:
    """内核可用策略名。

    只扫插件目录的文件名（策略名 = 类名蛇形 = 模块名），不在 API 进程 import 内核——
    内核依赖 pandas/pymongo，import 进来就违反「后端零计算」这条线。
    """
    folder = Path(settings.backtest_root) / "stock_selector" / "strategies"
    if not folder.is_dir():
        return []
    return sorted(p.stem for p in folder.glob("*.py") if not p.stem.startswith("_"))


def options() -> dict:
    """表单初始化数据：可选策略 + 参数缺省与区间。"""
    return {"strategies": list_strategies(), **form_spec()}


async def _with_rule_set_snapshot(payload: dict) -> dict:
    """带 rule_set_id 时由服务端注入判据串。

    前端只提交 id：串若由页面传来，就等于「作业参数里记的这套规则」可以伪造，
    回测结论和规则集页面对不上。快照取的是那一刻 rule_sets 行里的真实表达式。
    """
    if payload.get("sell_rule") or payload.get("add_rule"):
        raise BizError("卖出/加仓判据串不接受页面提交，请改用 rule_set_id", code=400, status_code=400)
    if not payload.get("rule_set_id"):
        return payload
    if payload.get("strategy"):
        raise BizError("策略模式不消费规则集的卖出/加仓串，请先清空策略", code=400, status_code=400)
    snap = await ruleset_service.snapshot_by_id(payload["rule_set_id"])
    return {**payload, "sell_rule": snap["sell"], "add_rule": snap["add"]}


async def start_run(user_id: str, params: dict | None, source: str = "manual") -> dict:
    if not user_id:
        raise BizError("user_id 不能为空", code=400, status_code=400)
    payload = validate(params)
    payload = await _with_rule_set_snapshot(payload)
    if user_id in _inflight:
        raise BizError(f"已有回测任务运行中: {_inflight[user_id]}", code=409, status_code=409)
    inflight = await backtest_jobs_repo.find_inflight(user_id)
    if inflight:
        raise BizError(f"已有回测任务运行中: {inflight['job_id']}", code=409, status_code=409)

    job_id = new_uuid()
    await backtest_jobs_repo.create({
        "job_id": job_id,
        "user_id": user_id,
        "status": "queued",
        "source": source,
        "params": json.dumps(payload, ensure_ascii=False),
    })
    try:
        # 按任务名投递：API 进程因此不需要 import worker 侧任务模块
        result = await asyncio.to_thread(
            celery_app.send_task, settings.backtest_task_name, args=[payload])
    except Exception as exc:  # noqa: BLE001  broker/redis 不可达
        message = f"任务投递失败: {exc}"[:512]
        await backtest_jobs_repo.update(job_id, {"status": "failed", "error": message})
        raise BizError(f"{message}（请确认 Celery worker 与 Redis 已启动）",
                       code=503, status_code=503) from exc

    await backtest_jobs_repo.update(job_id, {"celery_task_id": result.id})
    _inflight[user_id] = job_id
    return {"job_id": job_id, "celery_task_id": result.id, "status": "queued"}


def _read_celery(task_id: str) -> tuple[str, object]:
    """一次性读回状态与负载（redis 同步客户端，放到线程里跑）。"""
    ar = AsyncResult(task_id, app=celery_app)
    state = ar.state
    return state, (ar.info if state == "PROGRESS" else ar.result)


def _failure_message(value) -> str:
    if isinstance(value, dict):
        detail = value.get("exc_message") or ""
        if isinstance(detail, (list, tuple)):
            detail = "; ".join(str(x) for x in detail)
        return f"{value.get('exc_type') or 'TaskError'}: {detail}"[:512]
    return str(value)[:512]


def _age_seconds(job: dict) -> float:
    value = job.get("updated_at") or job.get("created_at")
    if not isinstance(value, datetime):
        return 0.0
    if value.tzinfo is None:      # SQLite 读回来是 naive UTC
        value = value.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - value).total_seconds()


async def _sync_job(job: dict) -> dict:
    """把 Celery 状态归并回作业表：只在读接口触发，没人查看时不写库。"""
    if job.get("status") in TERMINAL or not job.get("celery_task_id"):
        return job

    state, payload = await asyncio.to_thread(_read_celery, job["celery_task_id"])
    patch: dict = {}
    if state == "PROGRESS":
        meta = payload if isinstance(payload, dict) else {}
        patch = {"status": "running",
                 "progress": int(meta.get("progress") or 0),
                 "stage": str(meta.get("stage") or "")[:128]}
    elif state in ("STARTED", "RETRY"):
        patch = {"status": "running", "stage": "worker 已接收"}
    elif state == "SUCCESS":
        patch = {"status": "done", "progress": 100, "stage": "完成",
                 "result_summary": json.dumps(payload or {}, ensure_ascii=False, default=str)}
    elif state == "FAILURE":
        patch = {"status": "failed", "error": _failure_message(payload)}
    elif _age_seconds(job) > STALE_SECONDS:
        patch = {"status": "failed",
                 "error": f"{STALE_SECONDS // 3600} 小时内没有拿到任何进度，判定 worker 无响应"}
    if not patch:
        return job

    job = {**job, **patch, **(await backtest_jobs_repo.update(job["job_id"], patch) or {})}
    if patch["status"] in TERMINAL:
        if _inflight.get(job["user_id"]) == job["job_id"]:
            _inflight.pop(job["user_id"], None)
        await _notify(job, patch)
    return job


async def _notify(job: dict, patch: dict) -> None:
    if patch["status"] == "done":
        summary = _load_json(patch.get("result_summary"))
        metrics = summary.get("metrics") or {}
        event = "backtest_completed"
        title = "回测完成"
        content = (f"{summary.get('strategy', '')} "
                   f"{summary.get('start_date', '')}~{summary.get('end_date', '')}："
                   f"总收益 {_ratio_text(metrics.get('total_return'))}，"
                   f"成交 {summary.get('trade_count', 0)} 笔")
    else:
        event, title = "backtest_failed", "回测失败"
        content = patch.get("error", "")[:512]
    try:
        await inbox_notify.notify_user(job["user_id"], event, title, content,
                                       related_type="backtest_jobs", related_id=job["job_id"])
    except Exception:  # noqa: BLE001
        logger.exception("回测通知发送失败: %s", job["job_id"])


def _ratio_text(value) -> str:
    try:
        return f"{float(value):.2%}"
    except (TypeError, ValueError):
        return "-"


def _load_json(text) -> dict:
    if isinstance(text, dict):     # Mongo 后端可能直接存了 dict
        return text
    try:
        return json.loads(text or "") if text else {}
    except (TypeError, ValueError):
        return {}


def _decorate(job: dict, full: bool) -> dict:
    """把库里存的 JSON 字符串摊平成前端字段；列表页只给摘要，曲线与成交走详情接口。"""
    out = dict(job)
    out["params"] = _load_json(out.get("params"))
    summary = _load_json(out.get("result_summary"))
    out.pop("result_summary", None)
    if not summary:
        out["result"] = None
        return out
    out["result"] = summary if full else {
        k: v for k, v in summary.items() if k not in ("equity", "recent_trades", "params")
    }
    return out


async def job_detail(user_id: str, job_id: str) -> dict:
    job = await backtest_jobs_repo.get(job_id)
    if not job or job["user_id"] != user_id:
        raise NotFoundError("回测任务不存在")
    return _decorate(await _sync_job(job), full=True)


async def list_runs(user_id: str, page: int = 1, size: int = 20) -> dict:
    items, total = await backtest_jobs_repo.list_by_user(user_id, page, size)
    return {"items": [_decorate(await _sync_job(job), full=False) for job in items],
            "total": total}


def _ping_workers() -> list[str]:
    return list(celery_app.control.inspect(timeout=1.0).ping() or {})


async def worker_status() -> dict:
    """有哪些 worker 在线。作业卡在 queued 时，前端用它区分「在排队」和「没人消费」。"""
    try:
        names = sorted(await asyncio.to_thread(_ping_workers))
    except Exception as exc:  # noqa: BLE001  broker 不可达不该让页面 500
        return {"online": False, "workers": [], "error": str(exc)[:200]}
    return {"online": bool(names), "workers": names, "task_name": settings.backtest_task_name}
