"""交割单/交易记录：预览→提交双步、手动录入、删除、持仓联动（簿记）。"""
from __future__ import annotations

import uuid
from collections import Counter

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


async def _sync_holdings(user_id: str, derived: list[dict], keep: set[str] = frozenset()) -> list[str]:
    """把推导持仓写回 holdings：upsert 仍持有的，删除已平仓（qty→0）的推导持仓。
    仅删除 derived_from_trades=True 的行，手动持仓不受影响；keep 里的代码推导不出成本，原样保留。
    返回变更代码（有序）。"""
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
        if ex.get("derived_from_trades") and ex["code"] not in derived_codes and ex["code"] not in keep:
            await holdings_repo.delete(ex["id"])
    return sorted(derived_codes)


async def preview(user_id: str, raw, filename: str | None = None,
                  password: str = "") -> dict:
    """解析不落库（自动填 user_id/batch 空），返回 {total, valid, errors, items}。

    password 只用于解开加密 PDF，用完即弃：不落库、不进日志。
    """
    if not raw:
        raise BizError("请上传交割单文件")
    out = (parse_deal_bytes(raw, filename or "deal.csv", password)
           if isinstance(raw, (bytes, bytearray)) else parse_deal_text(raw))
    out.setdefault("skipped", 0)   # 只有 PDF 会区分「非买卖流水」，其余格式统一给 0
    return out


def _trade_sig(t: dict) -> tuple:
    """一笔成交的多重集指纹：同一天、同代码、同方向、同价量费 = 同一笔。"""
    return (str(t["trade_date"]), str(t["stock_code"]), str(t["direction"]),
            round(float(t["price"]), 4), int(t["quantity"]),
            round(float(t.get("amount", 0.0)), 2), round(float(t.get("commission", 0.0)), 2),
            round(float(t.get("stamp_tax", 0.0)), 2), round(float(t.get("transfer_fee", 0.0)), 2),
            round(float(t.get("settled_amount", 0.0)), 2))


async def commit(user_id: str, rows: list[dict], batch_note: str = "") -> dict:
    """校验重放 → 剔除与库内完全重复的行 → 写 trade_records → 移动加权推导 → 覆盖/新建 holdings。"""
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
        return {"imported": 0, "holdings_changed": [], "duplicates": 0,
                "errors": row_level_errors, "warnings": []}

    all_user_trades = await trades_repo.list_all_by_user(user_id)
    # 交割单查询区间经常一段一段重叠，整份再传一次会把成交翻倍、持仓推导成双倍数量（用户真实数据就中招过）。
    # 按多重集扣减：库里有一份就只挡一份，同一天同价同量的第二笔真单照样入库。
    seen = Counter(_trade_sig(t) for t in all_user_trades)
    fresh = []
    duplicates = 0
    for t in trades:
        sig = _trade_sig(t)
        if seen[sig] > 0:
            seen[sig] -= 1
            duplicates += 1
            continue
        fresh.append(t)

    batch_id = uuid.uuid4().hex[:12]
    for t in fresh:
        all_user_trades.append({**t, "source": "import", "batch_id": batch_id})

    holdings, unreliable = derive_holdings_from_trades(all_user_trades)

    for t in fresh:
        await trades_repo.create({**t, "user_id": user_id, "source": "import", "batch_id": batch_id})
    changed = await _sync_holdings(user_id, holdings, keep=set(unreliable))
    # 对账单只覆盖查询区间：区间开始前就持有的仓位推不出成本，这些整只不动既有持仓，只提示
    warnings = [f"{c} 出现卖出早于买入，交割单区间不含更早的买入，未推导其持仓" for c in unreliable]
    if duplicates:
        warnings.append(f"已跳过 {duplicates} 笔与库内完全重复的成交")
    return {"imported": len(fresh), "holdings_changed": changed, "duplicates": duplicates,
            "errors": [], "warnings": warnings}


async def create_one(user_id: str, row: dict) -> dict:
    """手动录入单笔，同样推导更新持仓。"""
    t = {**_settled(row), "source": "manual", "batch_id": ""}
    all_user_trades = await trades_repo.list_all_by_user(user_id)
    holdings, unreliable = derive_holdings_from_trades(all_user_trades + [t])
    if unreliable:
        raise BizError("卖出超过持仓：" + "、".join(unreliable) + "，请先补上更早的买入或核对数量")
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
    holdings, unreliable = derive_holdings_from_trades(remaining)
    if unreliable:
        raise BizError("删除后会出现卖出早于买入：" + "、".join(unreliable))
    await trades_repo.delete(trade_id)
    await _sync_holdings(user_id, holdings)
    return True


async def list_trades(user_id: str, query: dict) -> dict:
    rows = await trades_repo.list_by_user(
        user_id, code=query.get("code"), direction=query.get("direction"),
        date_from=query.get("trade_date_from"), date_to=query.get("trade_date_to"))
    return {"items": rows, "total": len(rows)}
