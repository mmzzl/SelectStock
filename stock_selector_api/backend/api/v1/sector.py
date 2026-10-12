# api/v1/sector.py
from fastapi import APIRouter, Query

from core.dependencies import CurrentUserDep
from core.response import ok
from services import sector_service

router = APIRouter(prefix="/sector", tags=["板块热力图"])


@router.get("/heat", summary="板块热力图格子（近 N 交易日等权涨幅）")
async def heat(user: CurrentUserDep,
               window: int = Query(3, ge=1, le=250),
               kind: str = Query("all", pattern="^(all|industry|concept)$")):
    return ok(await sector_service.heat(window, kind))


@router.get("/board", summary="板块详情与成分股区间表现")
async def board(user: CurrentUserDep,
                board_code: str = Query(..., min_length=2, max_length=16),
                window: int = Query(3, ge=1, le=250)):
    return ok(await sector_service.board_detail(board_code, window))


@router.get("/kline", summary="个股日 K（前端 canvas 自绘）")
async def kline(user: CurrentUserDep,
                code: str = Query(..., min_length=6, max_length=6),
                days: int = Query(120, ge=20, le=500)):
    return ok(await sector_service.stock_kline(code, days))
