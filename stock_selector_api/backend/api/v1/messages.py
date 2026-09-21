# api/v1/messages.py
from fastapi import APIRouter, Query

from core.dependencies import CurrentUserDep
from core.exceptions import NotFoundError
from core.response import ok
from services import inbox_service

router = APIRouter(prefix="/messages", tags=["站内信"])


@router.get("", summary="站内信列表")
async def list_messages(user: CurrentUserDep,
                        page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100),
                        unread_only: bool = False, since_id: str | None = None):
    return ok(await inbox_service.list_messages(user.id, {
        "page": page, "size": size, "unread_only": unread_only, "since_id": since_id}))


@router.put("/read_all", summary="全部已读")
async def mark_all_read(user: CurrentUserDep):
    await inbox_service.mark_all_read(user.id)
    return ok(message="已全部标记已读")


@router.put("/{msg_id}/read", summary="标记已读")
async def mark_read(user: CurrentUserDep, msg_id: str):
    if not await inbox_service.mark_read(user.id, msg_id):
        raise NotFoundError("消息不存在")
    return ok(message="已读")
