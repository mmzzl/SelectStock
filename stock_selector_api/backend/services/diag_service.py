"""services/diag_service.py — 外部诊断批次（M3）：幂等批次键 / 单飞锁 / 重试3次退避 / failed 落库。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import time

from core.exceptions import BizError
from repositories import holdings_repo, portfolio_results, portfolio_runs
from services import portfolio_plugin

logger = logging.getLogger("diag_service")

_BACKOFF_BASE = 1.0        # 秒；重试间隔 2^n * base + 抖动
_MAX_ATTEMPTS = 3
_RUNNING = set()           # 运行中（user_id, natural_day）

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


async def _replay(existing: dict) -> dict:
    """幂等重放：命中已完成批次直接返回既有 run + 已落库明细。"""
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


async def _guard_or_replay(existing: dict, key: tuple) -> dict:
    if existing["status"] == "running" or key in _RUNNING:
        raise BizError("诊断运行中，勿重复触发", code=409, status_code=409)
    return await _replay(existing)


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


async def trigger_diag(user_id: str, natural_day: str) -> dict | None:
    """触发单用户外部诊断（幂等重放；无持仓 → None）。"""
    if not user_id:
        raise BizError("user_id 不能为空", code=400, status_code=400)
    key = (user_id, natural_day)
    batch = _batch_id(user_id, natural_day)

    existing = await portfolio_runs.find_by_key(user_id, natural_day, batch)
    if existing:
        return await _guard_or_replay(existing, key)

    holdings = await holdings_repo.list_enabled(user_id)
    if not holdings:
        return None

    async with _SINGLE_FLIGHT:
        existing = await portfolio_runs.find_by_key(user_id, natural_day, batch)
        if existing:
            return await _guard_or_replay(existing, key)
        run = await portfolio_runs.create({
            "batch_id": batch,
            "user_id": user_id,
            "natural_day": natural_day,
            "status": "queued",
            "held_code_count": len(holdings),
        })
    run_id = run["run_id"]
    _RUNNING.add(key)
    asyncio.create_task(_run_diag(run_id, user_id, natural_day, key))
    run = await portfolio_runs.get(run_id)
    return {"run_id": run_id, "status": run["status"]}


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
    except Exception as exc:  # noqa: BLE001
        logger.exception("外部诊断失败: %s", run_id)
        try:
            await portfolio_runs.update(run_id, {"status": "failed", "error": str(exc)[:512]})
        except Exception as uexc:  # noqa: BLE001
            logger.error("写入失败状态也失败: %s: %s", run_id, uexc)
    finally:
        _RUNNING.discard(key)


async def diag_detail(run_id: str) -> dict | None:
    run = await portfolio_runs.get(run_id)
    if not run:
        return None
    results = await portfolio_results.list_by_run(run_id)
    run["results"] = results
    return run