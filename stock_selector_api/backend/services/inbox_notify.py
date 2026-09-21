"""事件→站内信落库 + WS 实时推送（双路，不依赖外部渠道开关）。"""
from __future__ import annotations

from repositories import inbox_repo
from services import inbox_service
from services.ws_manager import ws_manager


async def notify_user(user_id: str, event_code: str, title: str, content: str,
                      related_type: str = "", related_id: str = "") -> dict:
    msg = await inbox_repo.create({
        "user_id": user_id, "event_code": event_code, "title": title,
        "content": content, "related_type": related_type, "related_id": related_id,
    })
    out = inbox_service._out(msg)
    await ws_manager.push(user_id, {"type": "msg", "message": out})
    return out
