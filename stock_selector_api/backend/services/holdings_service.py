"""services/holdings_service.py — 持仓 CRUD + 上限 + CSV 导入（REQ-018）。"""
from __future__ import annotations

import csv
import io
import logging
import re
from datetime import date, datetime

from core.exceptions import BizError
from repositories import holdings_repo, portfolio_results, portfolio_runs, trades_repo
from services import portfolio_plugin

logger = logging.getLogger("holdings_service")

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
    rows = await _attach_diag_snapshot(user_id, rows)
    return await _attach_live_quotes(rows)


async def _latest_run(user_id: str) -> dict | None:
    """取「最近算出来的那次诊断」，不是「最近建的那一行」。

    同一天可能有两个 run：18:20 定时/刷新按钮走 `diag_service`（按 (用户, 日期) 幂等，原地重跑
    复用旧行、只把 updated_at 推到当下），外部按日期触发的走 `portfolio_service`（按持仓快照
    哈希幂等，会新建行）。只按 created_at 取第一行时，点了「刷新诊断」后持仓页仍然读着那条
    更晚建、但更早算出来的旧 run，看起来像按钮没生效。
    """
    runs, _ = await portfolio_runs.list_by_user(user_id, page=1, size=20)
    if not runs:
        return None
    return max(runs, key=_recency)


def _recency(run: dict) -> datetime:
    """created_at/updated_at 里较新的那个；缺时间戳的行排在最旧。"""
    stamps = [run.get(k) for k in ("created_at", "updated_at") if isinstance(run.get(k), datetime)]
    return max(stamps) if stamps else datetime.min


async def _attach_diag_snapshot(user_id: str, rows: list[dict]) -> list[dict]:
    """最近一次诊断 run 明细附到持仓行（latest_price/holding_pnl/pnl_pct/action）。
    无诊断 run 时原样返回（R1-2：后端不计算，仅透传插件落库的快照）。"""
    if not rows:
        return rows
    latest = await _latest_run(user_id)
    if not latest:
        return rows
    results = await portfolio_results.list_by_run(latest["run_id"])
    by_code = {r["code"]: r for r in results}
    for h in rows:
        snap = by_code.get(h["code"])
        for key in ("latest_price", "holding_pnl", "pnl_pct", "action", "action_reason",
                    "suggestion", "sell_trigger_price"):
            h[key] = snap.get(key) if snap else None
        h["diag_run_id"] = latest["run_id"]
        # 快照里的价是那次诊断（通常是前一天 18:00 之后）取的，不标出来就会被当成最新价读
        h["price_time"] = latest.get("natural_day") or ""
        h["price_source"] = "snapshot"
    return rows


async def _attach_live_quotes(rows: list[dict]) -> list[dict]:
    """现价改问插件（批量行情，今天实时优先），快照退成兜底。

    原来持仓页只显示诊断落库的那个数：日线 parquet 每天 18:00 才重建，收盘前打开页面看到的
    永远是上一交易日收盘，「今天跌了 9% 而页面纹丝不动」就是这么来的。
    插件不可达/没有行情时整页照常返回快照价，并把来源标成 snapshot，页面能显示它是旧价。
    """
    if not rows:
        return rows
    try:
        out = await portfolio_plugin.call_plugin_quotes({
            "natural_day": date.today().isoformat(),
            "holdings": [{"code": r["code"], "name": r.get("name", ""),
                          "quantity": r["quantity"], "cost_price": r["cost_price"]}
                         for r in rows]})
    except Exception as exc:            # 行情服务抖动不能让持仓页 500
        logger.warning("持仓现价刷新失败，退回诊断快照: %s", exc)
        return rows
    by_code = {str(r["code"]): r for r in out.get("results", [])}
    for h in rows:
        q = by_code.get(str(h["code"]))
        if not q or q.get("latest_price") is None:
            continue
        for key in ("latest_price", "holding_pnl", "pnl_pct", "market_value"):
            if key in q:
                h[key] = q[key]
        h["price_time"] = q.get("price_time") or ""
        h["price_source"] = q.get("price_source") or ""
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