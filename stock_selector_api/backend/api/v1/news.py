# api/v1/news.py
from fastapi import APIRouter, Query

from core.dependencies import CurrentUserDep
from core.response import ok
from services import news_service

router = APIRouter(prefix="/news", tags=["消息面"])


@router.get("/daily", summary="当日消息面（利好/利空/风险 + 与持仓相关的标记）")
async def daily(user: CurrentUserDep, day: str | None = Query(None, max_length=10)):
    return ok(await news_service.daily_for_user(user.id, day))


@router.get("/feed", summary="原始消息流（东财 10 秒线，倒序分页）")
async def feed(user: CurrentUserDep,
               limit: int = Query(30, ge=1, le=100),
               before: str = Query("", max_length=24, description="上一页末条 id；留空从最新开始")):
    return ok(await news_service.feed(limit, before or None))


@router.get("/item", summary="一条原始消息的详情（标题 + 摘要 + 关联标的）")
async def item(user: CurrentUserDep, id: str = Query(..., min_length=24, max_length=24)):
    return ok(await news_service.item(id))

