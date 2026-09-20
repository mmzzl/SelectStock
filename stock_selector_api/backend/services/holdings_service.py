"""services/holdings_service.py — 持仓 CRUD + 上限 + CSV 导入（REQ-018）。"""
from __future__ import annotations

import csv
import io
import re

from core.exceptions import BizError
from repositories import holdings_repo, portfolio_results, portfolio_runs, trades_repo
from services import portfolio_plugin

MAX_HOLDINGS = 200
_CODE_RE = re.compile(r"^\d{6}$")


async def create_holding(user_id: str, data: dict) -> dict:
    if await holdings_repo.count_by_user(user_id) >= MAX_HOLDINGS:
        raise BizError(f"持仓已达 {MAX_HOLDINGS} 条上限")
    if await holdings_repo.find_by_user_code(user_id, data["code"]):
        raise BizError("该股票已在持仓列表中")
    return await holdings_repo.create({"user_id": user_id, **data})


async def list_holdings(user_id: str, enabled_only: bool = False) -> list[dict]:
    if enabled_only:
        rows = await holdings_repo.list_enabled(user_id)
    else:
        rows = await holdings_repo.list_by_user(user_id)
    return await _attach_diag_snapshot(user_id, rows)


async def _attach_diag_snapshot(user_id: str, rows: list[dict]) -> list[dict]:
    """最近一次诊断 run 明细附到持仓行（latest_price/holding_pnl/pnl_pct/action）。
    无诊断 run 时原样返回（R1-2：后端不计算，仅透传插件落库的快照）。"""
    if not rows:
        return rows
    runs, _ = await portfolio_runs.list_by_user(user_id, page=1, size=1)
    if not runs:
        return rows
    latest = runs[0]
    results = await portfolio_results.list_by_run(latest["run_id"])
    by_code = {r["code"]: r for r in results}
    for h in rows:
        snap = by_code.get(h["code"])
        for key in ("latest_price", "holding_pnl", "pnl_pct", "action", "action_reason", "suggestion"):
            h[key] = snap.get(key) if snap else None
        h["diag_run_id"] = latest["run_id"]
    return rows


async def pnl(user_id: str, natural_day: str) -> dict:
    """利润分析：透传持仓诊断插件 pnl 单点口径（后端零算术，R1-2）。"""
    trades = await trades_repo.list_all_by_user(user_id)
    holdings = await holdings_repo.list_enabled(user_id)
    return await portfolio_plugin.call_plugin_pnl({
        "user_id": user_id,
        "natural_day": natural_day,
        "trades": trades,
        "holdings": holdings,
    })


async def find_by_user_code(user_id: str, code: str) -> dict | None:
    return await holdings_repo.find_by_user_code(user_id, code)


async def update_holding(user_id: str, holding_id: str, data: dict) -> dict:
    h = await holdings_repo.get(holding_id)
    if not h or h["user_id"] != user_id:
        raise BizError("持仓不存在", code=404, status_code=404)
    return await holdings_repo.update(holding_id, data)


async def delete_holding(user_id: str, holding_id: str) -> None:
    h = await holdings_repo.get(holding_id)
    if not h or h["user_id"] != user_id:
        raise BizError("持仓不存在", code=404, status_code=404)
    await holdings_repo.delete(holding_id)


async def delete_all(user_id: str) -> int:
    """清空某用户全部持仓（测试与导入替换用）。"""
    rows = await holdings_repo.list_by_user(user_id)
    for r in rows:
        await holdings_repo.delete(r["id"])
    return len(rows)


def _validate_row(row: dict, errs: list[str], lineno: int) -> dict | None:
    code = str(row.get("code", "")).strip()
    if not _CODE_RE.match(code):
        errs.append(f"第{lineno}行: code 需为6位数字")
        return None
    try:
        quantity = int(str(row.get("quantity", "")).strip())
    except (TypeError, ValueError):
        quantity = -1
    if quantity < 0:
        errs.append(f"第{lineno}行: quantity 需为非负整数")
        return None
    try:
        cost = round(float(str(row.get("cost_price", "")).strip()), 4)
    except (TypeError, ValueError):
        errs.append(f"第{lineno}行: cost_price 需为数字")
        return None
    if cost < 0:
        errs.append(f"第{lineno}行: cost_price 需非负")
        return None
    return {"code": code, "name": str(row.get("name", ""))[:64],
            "quantity": quantity, "cost_price": cost}


def parse_import(text: str, strategy: str) -> dict:
    """解析券商 CSV：表头 code,name,quantity,cost_price。逐行校验，错误不中断。"""
    if strategy not in ("upsert", "skip"):
        raise BizError("strategy 需为 upsert|skip")
    reader = csv.DictReader(io.StringIO(text.strip()))
    headers = {h.strip() for h in reader.fieldnames} if reader.fieldnames else set()
    if headers != {"code", "name", "quantity", "cost_price"}:
        raise BizError("CSV 表头需为 code,name,quantity,cost_price")
    result = {"total": 0, "valid": 0, "updated": 0, "skipped": 0, "errors": [], "_items": []}
    for lineno, row in enumerate(reader, start=2):
        result["total"] += 1
        item = _validate_row(row, result["errors"], lineno)
        if item is None:
            continue
        result["valid"] += 1
        result["_items"].append(item)
    return result


async def apply_import(user_id: str, text: str, strategy: str) -> dict:
    """解析并落库：upsert=已存在则更新，skip=已存在跳过；超上限拒绝。"""
    parsed = parse_import(text, strategy)
    if parsed["errors"]:
        return {k: v for k, v in parsed.items() if k != "_items"}
    if await holdings_repo.count_by_user(user_id) + parsed["valid"] > MAX_HOLDINGS:
        raise BizError(f"导入将超出 {MAX_HOLDINGS} 条上限")
    for it in parsed["_items"]:
        existing = await holdings_repo.find_by_user_code(user_id, it["code"])
        if existing and strategy == "skip":
            parsed["skipped"] += 1
            continue
        if existing:
            await holdings_repo.update(existing["id"], it)
            parsed["updated"] += 1
        else:
            await holdings_repo.create({"user_id": user_id, **it})
            parsed["updated"] += 1
    return {k: v for k, v in parsed.items() if k != "_items"}