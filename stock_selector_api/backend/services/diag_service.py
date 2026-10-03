"""services/diag_service.py — 外部诊断批次（M3）：幂等批次键 / 单飞锁 / 重试3次退避 / failed 落库。

「刷新诊断」是刷新入口：同一天的批次在 `_REFRESH_COOLDOWN_MINUTES` 内幂等重放旧结果，
超过冷却才原地重跑，既能让用户主动刷新行情，又不会连点把插件打爆。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import time
from datetime import datetime, timezone

from core.exceptions import BizError
from repositories import holdings_repo, portfolio_results, portfolio_runs
from services import inbox_notify, portfolio_plugin

logger = logging.getLogger("diag_service")

_BACKOFF_BASE = 1.0        # 秒；重试间隔 2^n * base + 抖动
_MAX_ATTEMPTS = 3
_RUNNING = set()           # 运行中（user_id, natural_day）
_REFRESH_COOLDOWN_MINUTES = 10.0   # 同一天两次「刷新诊断」的最小间隔，防止连点把插件打爆

# pytest-asyncio 默认 function 级事件循环：跨用例复用同一把锁会绑定到已销毁的 loop。
# reset_runtime() 每次重建，保证每个测试循环拿到全新的、归属于当前 loop 的锁。
_SINGLE_FLIGHT: asyncio.Lock = asyncio.Lock()


async def reset_runtime() -> None:
    """清空运行中集合 + 重建单飞锁（测试/重启用）。"""
    global _SINGLE_FLIGHT
    _RUNNING.clear()
    _SINGLE_FLIGHT = asyncio.Lock()


def _batch_id(user_id: str, natural_day: str) -> str:
    raw = f"portfolio_diag:{user_id}:{natural_day}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _minutes_since(run: dict) -> float | None:
    """距上次诊断落定的分钟数；时间戳缺失/类型不对 → None（视为已过期，允许刷新）。

    updated_at 以 UTC naive 入库（见 db/sql/base.py::utcnow），比较前补回 tzinfo，
    免得跑在 UTC+8 机器上拿本地时间做减法，把 8 小时时差错算成「早就该刷新了」。
    """
    ts = run.get("updated_at")
    if not isinstance(ts, datetime):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 60.0


async def _replay(existing: dict) -> dict:
    """幂等重放：命中冷却期内的已完成批次，直接返回既有 run + 已落库明细。"""
    results = await portfolio_results.list_by_run(existing["run_id"])
    return {
        "run_id": existing["run_id"],
        "status": existing["status"],
        "results": results,
        "summary": {
            "action_counts": {},
            "total_market_value": existing.get("total_market_value", 0.0),
            "total_pnl": existing.get("total_pnl", 0.0),
            "holding_count": existing.get("held_code_count", 0),
        },
    }


async def _rerun(existing: dict, user_id: str, natural_day: str) -> dict:
    """原地重跑：清掉上一批明细，复用同一个 run（(user_id, natural_day) 唯一约束）。"""
    await portfolio_results.delete_by_run(existing["run_id"])
    await portfolio_runs.update(existing["run_id"], {"status": "queued", "error": ""})
    return await _dispatch(existing["run_id"], user_id, natural_day)


async def _guard_or_replay(existing: dict, user_id: str, natural_day: str) -> dict:
    """命中既有批次：running → 拒；failed 或已过刷新冷却 → 原地重跑；否则幂等重放。"""
    key = (user_id, natural_day)
    if existing["status"] == "running" or key in _RUNNING:
        raise BizError("诊断运行中，勿重复触发", code=409, status_code=409)
    if existing["status"] == "failed":
        return await _rerun(existing, user_id, natural_day)
    age = _minutes_since(existing)
    if age is not None and age < _REFRESH_COOLDOWN_MINUTES:
        return await _replay(existing)
    return await _rerun(existing, user_id, natural_day)


async def _retry_call(payload: dict) -> dict:
    last_err: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return await portfolio_plugin.call_plugin_analyze(payload)
        except Exception as exc:  # noqa: BLE001 重试一切可恢复异常
            last_err = exc
            if attempt == _MAX_ATTEMPTS:
                break
            delay = (2 ** attempt) * _BACKOFF_BASE + random.uniform(0, 0.3)
            await asyncio.sleep(delay)
    raise last_err  # type: ignore[misc]


async def _dispatch(run_id: str, user_id: str, natural_day: str) -> dict:
    """挂后台执行一次诊断，立即返回 queued。"""
    key = (user_id, natural_day)
    _RUNNING.add(key)
    asyncio.create_task(_run_diag(run_id, user_id, natural_day, key))
    run = await portfolio_runs.get(run_id)
    return {"run_id": run_id, "status": run["status"]}


async def trigger_diag(user_id: str, natural_day: str) -> dict | None:
    """触发单用户外部诊断（冷却期内重放旧结果，过期则原地重跑；无持仓 → None）。"""
    if not user_id:
        raise BizError("user_id 不能为空", code=400, status_code=400)
    batch = _batch_id(user_id, natural_day)

    existing = await portfolio_runs.find_by_key(user_id, natural_day, batch)
    if existing:
        return await _guard_or_replay(existing, user_id, natural_day)

    holdings = await holdings_repo.list_enabled(user_id)
    if not holdings:
        return None

    async with _SINGLE_FLIGHT:
        existing = await portfolio_runs.find_by_key(user_id, natural_day, batch)
        if existing:
            return await _guard_or_replay(existing, user_id, natural_day)
        run = await portfolio_runs.create({
            "batch_id": batch,
            "user_id": user_id,
            "natural_day": natural_day,
            "status": "queued",
            "held_code_count": len(holdings),
        })
    return await _dispatch(run["run_id"], user_id, natural_day)


async def _run_diag(run_id: str, user_id: str, natural_day: str, key: tuple) -> None:
    try:
        holdings = await holdings_repo.list_enabled(user_id)
        await portfolio_runs.update(run_id, {"status": "running"})
        t0 = time.time()
        plugin_out = await _retry_call({
            "user_id": user_id,
            "natural_day": natural_day,
            "holdings": holdings,
        })
        plugin_ms = int((time.time() - t0) * 1000)
        for r in plugin_out["results"]:
            await portfolio_results.create({"run_id": run_id, **r})
        summary = plugin_out.get("summary", {})
        await portfolio_runs.update(run_id, {
            "status": "completed",
            "action_count": len(plugin_out["results"]),
            "total_market_value": float(summary.get("total_market_value", 0.0)),
            "total_pnl": float(summary.get("total_pnl", 0.0)),
            "plugin_time_ms": plugin_ms,
        })
        await inbox_notify.notify_user(
            user_id, "portfolio_diag_completed", "持仓诊断完成",
            f"共诊断 {len(plugin_out['results'])} 只持仓，建议卖出 "
            f"{summary.get('action_counts', {}).get('sell', 0)} 只",
            related_type="portfolio_diag_runs", related_id=run_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("外部诊断失败: %s", run_id)
        try:
            await portfolio_runs.update(run_id, {"status": "failed", "error": str(exc)[:512]})
        except Exception as uexc:  # noqa: BLE001
            logger.error("写入失败状态也失败: %s: %s", run_id, uexc)
        try:
            await inbox_notify.notify_user(
                user_id, "portfolio_diag_failed", "持仓诊断失败", str(exc)[:512],
                related_type="portfolio_diag_runs", related_id=run_id)
        except Exception:  # noqa: BLE001
            logger.exception("诊断失败通知发送失败: %s", run_id)
    finally:
        _RUNNING.discard(key)


async def diag_detail(run_id: str) -> dict | None:
    run = await portfolio_runs.get(run_id)
    if not run:
        return None
    results = await portfolio_results.list_by_run(run_id)
    run["results"] = results
    return run