# api/v1/news.py
from fastapi import APIRouter, Query

from core.dependencies import CurrentUserDep
from core.response import ok
from services import news_service

router = APIRouter(prefix="/news", tags=["消息面"])


@router.get("/daily", summary="当日消息面（利好/利空/风险 + 与持仓相关的标记）")
async def daily(user: CurrentUserDep, day: str | None = Query(None, max_length=10)):
    return ok(await news_service.daily_for_user(user.id, day))
