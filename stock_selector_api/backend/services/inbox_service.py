# services/inbox_service.py —— 组装/出参（读模型加 read_status 语义字段）
from repositories import inbox_repo


def _out(msg: dict) -> dict:
    # read_status: True=未读（read_at is None）——对齐客户端 unread 判定
    return {**msg, "read_status": msg.get("read_at") is None}


async def list_messages(user_id: str, query: dict) -> dict:
    items, total, unread = await inbox_repo.list_for(
        user_id, page=query.get("page", 1), size=query.get("size", 20),
        unread_only=bool(query.get("unread_only")), since_id=query.get("since_id"))
    return {"items": [_out(m) for m in items], "total": total, "unread_count": unread}


async def mark_read(user_id: str, msg_id: str) -> bool:
    return await inbox_repo.mark_read(msg_id, user_id)


async def mark_all_read(user_id: str) -> None:
    await inbox_repo.mark_all_read(user_id)
