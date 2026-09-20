"""api/v1/holdings.py — 持仓管理（当前用户属主，隔离红线 R3-1）。"""
from __future__ import annotations

from fastapi import APIRouter, Request

from core.dependencies import CurrentUserDep
from core.response import ok
from schemas.holdings import HoldingIn, HoldingUpdate
from services import holdings_service

router = APIRouter(prefix="/holdings", tags=["持仓"])


@router.get("", summary="当前用户持仓")
async def list_holdings(user: CurrentUserDep):
    rows = await holdings_service.list_holdings(user.id)
    return ok(rows)


@router.get("/pnl", summary="利润分析（代理持仓诊断插件 pnl，零算术）")
async def holdings_pnl(user: CurrentUserDep, natural_day: str):
    data = await holdings_service.pnl(user.id, natural_day)
    return ok(data)


@router.post("", summary="新增持仓")
async def create_holding(user: CurrentUserDep, body: HoldingIn):
    h = await holdings_service.create_holding(user.id, body.model_dump())
    return ok(h, message="持仓已添加")


@router.put("/{holding_id}", summary="更新持仓")
async def update_holding(user: CurrentUserDep, holding_id: str, body: HoldingUpdate):
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    h = await holdings_service.update_holding(user.id, holding_id, data)
    return ok(h, message="持仓已更新")


@router.delete("/{holding_id}", summary="删除持仓")
async def delete_holding(user: CurrentUserDep, holding_id: str):
    await holdings_service.delete_holding(user.id, holding_id)
    return ok(message="持仓已删除")


@router.post("/import", summary="CSV 导入持仓")
async def import_holdings(user: CurrentUserDep, request: Request, strategy: str = "upsert"):
    text = (await request.body()).decode("utf-8")
    result = await holdings_service.apply_import(user.id, text, strategy)
    return ok(result, message="导入完成")