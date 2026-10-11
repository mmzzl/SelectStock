"""持仓诊断服务（M1）：代理插件 + 双表落库 + 幂等重放 + 按用户隔离列表。

红线：
- R1-1/R1-2 判定与收益全部透传插件返回，后端不做任何判定/收益公式；
- R2-3 同 (user_id, natural_day, holdings_md5) 幂等复用 run_id；
- R3-1 一律按 user_id 过滤（隔离）。
"""

import hashlib
import json
import logging
import time

from core.exceptions import BizError
from repositories import portfolio_results, portfolio_runs
from services import portfolio_plugin

logger = logging.getLogger("portfolio_service")

_RESULT_FIELDS = (
    "code", "name", "action", "action_reason", "latest_price",
    "quantity", "holding_pnl", "pnl_pct", "suggestion", "sell_trigger_price",
)


def holdings_md5(holdings: list[dict]) -> str:
    """规范化持仓快照（排序 + 固定字段）算 md5，作为批次幂等键（batch_id）。"""
    canon = json.dumps(
        [
            {
                "code": h["code"],
                "name": h.get("name", ""),
                "quantity": int(h["quantity"]),
                "cost_price": float(h["cost_price"]),
            }
            for h in holdings
        ],
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.md5(canon.encode("utf-8")).hexdigest()


def _result_out(r: dict) -> dict:
    return {k: r.get(k) for k in _RESULT_FIELDS}


def _summary_from_plugin(plugin_out: dict, holding_count: int) -> dict:
    summary = dict(plugin_out.get("summary", {}))
    summary.setdefault("action_counts", {})
    summary.setdefault("total_market_value", 0.0)
    summary.setdefault("total_pnl", 0.0)
    summary.setdefault("holding_count", holding_count)
    return summary


def _summary_from_run(run: dict, results: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for r in results:
        counts[r["action"]] = counts.get(r["action"], 0) + 1
    return {
        "action_counts": counts,
        "total_market_value": run.get("total_market_value", 0.0),
        "total_pnl": run.get("total_pnl", 0.0),
        "holding_count": run.get("held_code_count", 0),
    }


async def analyze(user_id: str, natural_day: str, holdings: list[dict]) -> dict:
    """代理插件执行一次诊断：幂等重放 / 建 run / 落库。返回 {run_id, status, results, summary}。"""
    batch_id = holdings_md5(holdings)

    existing = await portfolio_runs.find_by_key(user_id, natural_day, batch_id)
    if existing:
        results = await portfolio_results.list_by_run(existing["run_id"])
        return {
            "run_id": existing["run_id"],
            "status": existing["status"],
            "results": [_result_out(r) for r in results],
            "summary": _summary_from_run(existing, results),
        }

    run = await portfolio_runs.create({
        "batch_id": batch_id, "user_id": user_id, "natural_day": natural_day,
        "status": "queued", "held_code_count": len(holdings),
    })
    run_id = run["run_id"]
    try:
        t0 = time.time()
        plugin_out = await portfolio_plugin.call_plugin_analyze(
            {"user_id": user_id, "natural_day": natural_day, "holdings": holdings}
        )
        plugin_ms = int((time.time() - t0) * 1000)
        results = plugin_out["results"]
        for r in results:
            await portfolio_results.create({"run_id": run_id, **r})
        summary = plugin_out.get("summary", {})
        await portfolio_runs.update(run_id, {
            "status": "completed",
            "action_count": len(results),
            "total_market_value": float(summary.get("total_market_value", 0.0)),
            "total_pnl": float(summary.get("total_pnl", 0.0)),
            "plugin_time_ms": plugin_ms,
        })
        return {
            "run_id": run_id,
            "status": "completed",
            "results": [_result_out(r) for r in results],
            "summary": _summary_from_plugin(plugin_out, len(results)),
        }
    except Exception as exc:
        logger.exception("持仓诊断插件调用失败: %s", run_id)
        await portfolio_runs.update(run_id, {"status": "failed", "error": str(exc)[:512]})
        raise BizError("持仓诊断失败：插件不可用", code=502, status_code=502) from exc


async def list_runs(user_id: str, page: int = 1, size: int = 20):
    return await portfolio_runs.list_by_user(user_id, page, size)