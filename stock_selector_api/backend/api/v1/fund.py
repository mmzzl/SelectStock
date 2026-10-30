# api/v1/fund.py
from fastapi import APIRouter, Query

from core.dependencies import CurrentUserDep
from core.response import ok
from services import fund_service

router = APIRouter(prefix="/fund", tags=["主力资金榜"])


@router.get("/rank", summary="最近 N 个交易日主力净流入/流出榜（预计算薄代理）")
async def rank(user: CurrentUserDep,
               window: int = Query(3, ge=1, le=250),
               side: str = Query("in", pattern="^(in|out)$"),
               limit: int = Query(50, ge=1, le=200)):
    return ok(await fund_service.rank(window, side, limit))
