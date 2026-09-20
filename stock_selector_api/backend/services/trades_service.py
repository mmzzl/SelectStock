"""交割单/交易记录：预览→提交双步、手动录入、删除、持仓联动（簿记）。"""
from __future__ import annotations

import uuid

from core.exceptions import BizError
from repositories import holdings_repo, trades_repo
from services.deal_parser import parse_deal_bytes, parse_deal_text
from services.derive_holdings import derive_holdings_from_trades


def _settled(row: dict) -> dict:
    """把 TradeRow/手动入参归整为落库字段（batch_id 由调用方补）。"""
    return {
        "trade_date": str(row["trade_date"]),
        "stock_code": str(row["stock_code"]),
        "stock_name": str(row.get("stock_name", ""))[:64],
        "direction": str(row["direction"]),
        "price": round(float(row["price"]), 4),
        "quantity": int(row["quantity"]),
        "amount": round(float(row.get("amount", 0.0)), 2),
        "commission": round(float(row.get("commission", 0.0)), 2),
        "stamp_tax": round(float(row.get("stamp_tax", 0.0)), 2),
        "transfer_fee": round(float(row.get("transfer_fee", 0.0)), 2),
        "settled_amount": round(float(row.get("settled_amount", 0.0)), 2),
    }


async def _sync_holdings(user_id: str, derived: list[dict]) -> list[str]:
    """把推导持仓写回 holdings：upsert 仍持有的，删除已平仓（qty→0）的推导持仓。
    仅删除 derived_from_trades=True 的行，手动持仓不受影响。返回变更代码（有序）。"""
    derived_codes = {h["stock_code"] for h in derived}
    for h in derived:
        existing = await holdings_repo.find_by_user_code(user_id, h["stock_code"])
        if existing:
            await holdings_repo.update(existing["id"], {
                "quantity": h["quantity"], "cost_price": h["cost_price"],
                "name": h["stock_name"] or existing["name"], "derived_from_trades": True,
            })
        else:
            await holdings_repo.create({
                "user_id": user_id, "code": h["stock_code"], "name": h["stock_name"],
                "quantity": h["quantity"], "cost_price": h["cost_price"],
                "derived_from_trades": True,
            })
    for ex in await holdings_repo.list_by_user(user_id):
        if ex.get("derived_from_trades") and ex["code"] not in derived_codes:
            await holdings_repo.delete(ex["id"])
    return sorted(derived_codes)


async def preview(user_id: str, raw, filename: str | None = None) -> dict:
    """解析不落库（自动填 user_id/batch 空），返回 {total, valid, errors, items}。"""
    if not raw:
        raise BizError("请上传交割单文件")
    if isinstance(raw, (bytes, bytearray)):
        return parse_deal_bytes(raw, filename or "deal.csv")
    return parse_deal_text(raw)


async def commit(user_id: str, rows: list[dict], batch_note: str = "") -> dict:
    """校验重放 → 写 trade_records → 移动加权推导 → 覆盖/新建 holdings。"""
    if not rows:
        raise BizError("无可提交的记录")
    trades = []
    row_level_errors = []
    for i, row in enumerate(rows, start=1):
        try:
            trades.append(_settled(row))
        except (KeyError, TypeError, ValueError):
            row_level_errors.append({"line": i, "reason": "字段缺失或非法"})
    if row_level_errors:
        return {"imported": 0, "holdings_changed": [], "errors": row_level_errors}

    all_user_trades = await trades_repo.list_all_by_user(user_id)
    batch_id = uuid.uuid4().hex[:12]
    for t in trades:
        all_user_trades.append({**t, "source": "import", "batch_id": batch_id})
    all_user_trades.sort(key=lambda t: (t["stock_code"], t["trade_date"]))

    holdings, derive_errs = derive_holdings_from_trades(all_user_trades)
    if derive_errs:
        return {"imported": 0, "holdings_changed": [], "errors": [{"line": 0, "reason": e} for e in derive_errs]}

    for t in trades:
        await trades_repo.create({**t, "user_id": user_id, "source": "import", "batch_id": batch_id})
    changed = await _sync_holdings(user_id, holdings)
    return {"imported": len(trades), "holdings_changed": changed, "errors": []}


async def create_one(user_id: str, row: dict) -> dict:
    """手动录入单笔，同样推导更新持仓。"""
    t = {**_settled(row), "source": "manual", "batch_id": ""}
    all_user_trades = await trades_repo.list_all_by_user(user_id)
    trial = sorted(all_user_trades + [t], key=lambda r: (r["stock_code"], r["trade_date"]))
    holdings, errs = derive_holdings_from_trades(trial)
    if errs:
        raise BizError("；".join(errs))
    created = await trades_repo.create({**t, "user_id": user_id})
    await _sync_holdings(user_id, holdings)
    return created


async def delete_one(user_id: str, trade_id: str) -> bool:
    """删除单笔并重算持仓（仅允许 manual 来源；import 批次行删除留待扩展）。"""
    row = await trades_repo.get(trade_id)
    if not row or row["user_id"] != user_id:
        raise BizError("交易记录不存在", code=404, status_code=404)
    if row["source"] != "manual":
        raise BizError("导入产生的记录不可单笔删除", code=400, status_code=400)
    remaining = [t for t in await trades_repo.list_all_by_user(user_id) if t["id"] != trade_id]
    holdings, errs = derive_holdings_from_trades(
        sorted(remaining, key=lambda r: (r["stock_code"], r["trade_date"])))
    if errs:
        raise BizError("；".join(errs))
    await trades_repo.delete(trade_id)
    await _sync_holdings(user_id, holdings)
    return True


async def list_trades(user_id: str, query: dict) -> dict:
    rows = await trades_repo.list_by_user(
        user_id, code=query.get("code"), direction=query.get("direction"),
        date_from=query.get("trade_date_from"), date_to=query.get("trade_date_to"))
    return {"items": rows, "total": len(rows)}
