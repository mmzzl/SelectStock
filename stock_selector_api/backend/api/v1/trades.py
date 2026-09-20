"""api/v1/trades.py — 交割单双步导入 + 交易记录（属主隔离，密码网关在 router 挂载）。"""
from __future__ import annotations

from fastapi import APIRouter, File, Request, UploadFile

from core.dependencies import CurrentUserDep
from core.exceptions import BizError
from core.response import fail, ok
from schemas.trades import TradeRow
from services import trades_service

router = APIRouter(prefix="/trades", tags=["交易记录"])


@router.post("/import/preview", summary="交割单预览（不落库）")
async def preview(request: Request, file: UploadFile | None = File(default=None)):
    if file is not None:
        raw = await file.read()
        filename = file.filename or "deal.csv"
    else:
        body = await request.body()
        raw = body.decode("utf-8") if body else ""
        filename = "deal.csv"
    out = await trades_service.preview("", raw, filename)   # 解析不依赖 user
    return ok(out)


@router.post("/import/commit", summary="提交交割单（写记录并推导持仓）")
async def commit(user: CurrentUserDep, request: Request):
    body = await request.json()
    rows = body.get("rows") or []
    try:
        valid = [TradeRow(**r).model_dump() for r in rows]
    except Exception as e:
        raise BizError(f"记录字段不合法: {e}", code=422, status_code=422)
    out = await trades_service.commit(user.id, valid, body.get("batch_note", ""))
    if out["errors"]:
        return fail("提交存在行级错误，未写入", data=out)
    return ok(out, message="导入完成")


@router.get("", summary="交易记录列表")
async def list_trades(user: CurrentUserDep, code: str | None = None,
                      direction: str | None = None,
                      trade_date_from: str | None = None,
                      trade_date_to: str | None = None):
    data = await trades_service.list_trades(user.id, {
        "code": code, "direction": direction,
        "trade_date_from": trade_date_from, "trade_date_to": trade_date_to})
    return ok(data)


@router.post("", summary="手动录入单笔")
async def create_one(user: CurrentUserDep, row: TradeRow):
    created = await trades_service.create_one(user.id, row.model_dump())
    return ok(created, message="已录入并更新持仓")


@router.delete("/{trade_id}", summary="删除单笔（仅手动来源）")
async def delete_one(user: CurrentUserDep, trade_id: str):
    await trades_service.delete_one(user.id, trade_id)
    return ok(message="记录已删除")
