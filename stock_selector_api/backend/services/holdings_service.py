"""services/holdings_service.py — 持仓维护：属性校验、上限 200、CSV 导入（行级容错）。"""
from __future__ import annotations

import csv
import io

from core.exceptions import BizError
from repositories import holdings_repo
from schemas.holdings import ImportResult

MAX_HOLDINGS = 200


def _validate_row(row: dict) -> str | None:
    if not row["code"]:
        return "股票代码为空"
    qty = row["quantity"]
    if qty is None or qty < 0:
        return f"数量非法:{row['quantity']!r}"
    cost = row["cost_price"]
    if cost is None or cost < 0:
        return f"成本价非法:{row['cost_price']!r}"
    return None


async def create_holding(user_id: str, data: dict) -> dict:
    if await holdings_repo.find_by_user_code(user_id, data["code"]):
        raise BizError("持仓已存在", code=400, status_code=400)
    if await holdings_repo.count_by_user(user_id) >= MAX_HOLDINGS:
        raise BizError("最多维护 200 只股票持仓", code=400, status_code=400)
    return await holdings_repo.create({"user_id": user_id, **data})


async def list_holdings(user_id: str) -> list[dict]:
    return await holdings_repo.list_by_user(user_id)


async def find_by_user_code(user_id: str, code: str) -> dict | None:
    return await holdings_repo.find_by_user_code(user_id, code)


async def update_holding(user_id: str, holding_id: str, data: dict) -> dict:
    if not await holdings_repo.get(holding_id):
        raise BizError("持仓不存在", code=404, status_code=404)
    return await holdings_repo.update(holding_id, data)


async def delete_holding(user_id: str, holding_id: str) -> None:
    if not await holdings_repo.delete(holding_id):
        raise BizError("持仓不存在", code=404, status_code=404)


async def delete_all(user_id: str) -> None:
    rows = await holdings_repo.list_by_user(user_id)
    for r in rows:
        await holdings_repo.delete(r["id"])


async def parse_import(text: str) -> ImportResult:
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise BizError("CSV 无数据行", code=400, status_code=400)
    if set(rows[0].keys()) != {"代码", "名称", "数量", "成本价"}:
        raise BizError("CSV 表头必须为:代码,名称,数量,成本价", code=400, status_code=400)
    result = ImportResult(total=len(rows))
    for i, r in enumerate(rows, start=2):
        try:
            code = (r["代码"] or "").strip()
            name = (r["名称"] or "").strip()
            quantity = int((r["数量"] or "").strip())
            cost = float((r["成本价"] or "").strip())
        except (TypeError, ValueError):
            result.errors.append(f"第{i}行:数值格式非法")
            continue
        err = _validate_row({"code": code, "quantity": quantity, "cost_price": cost})
        if err:
            result.errors.append(f"第{i}行:{err}")
            continue
        result.valid += 1
        result.items.append({"code": code, "name": name, "quantity": quantity, "cost_price": cost})
    return result


async def apply_import(user_id: str, parsed: ImportResult) -> ImportResult:
    if parsed.errors:
        return parsed
    if await holdings_repo.count_by_user(user_id) + parsed.valid > MAX_HOLDINGS:
        raise BizError("导入后将超过 200 只上限", code=400, status_code=400)
    result = ImportResult(total=parsed.total, valid=parsed.valid)
    for item in parsed.items:
        exist = await holdings_repo.find_by_user_code(user_id, item["code"])
        if exist:
            await holdings_repo.update(exist["id"], item)
        else:
            await holdings_repo.create({"user_id": user_id, **item})
        result.updated += 1
    return result