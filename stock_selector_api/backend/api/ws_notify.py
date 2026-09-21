"""WS 通知端点：首帧鉴权 → hello；在线用户实时收 msg 帧。"""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core import security
from repositories import inbox_repo
from services.ws_manager import ws_manager

router = APIRouter()


@router.websocket("/ws/notify")
async def ws_notify(ws: WebSocket) -> None:
    await ws.accept()
    try:
        first = await ws.receive_json()
    except Exception:                       # noqa: BLE001
        await ws.close(code=4401)
        return
    if first.get("type") != "auth":
        await ws.close(code=4401)
        return
    try:
        payload = security.decode_token(first.get("token", ""), expected_type="access")
        user_id = security._user_id_from_sub(payload["sub"])
    except Exception:                       # noqa: BLE001
        await ws.close(code=4401)
        return

    latest = await inbox_repo.latest_id(user_id)
    unread_count = await inbox_repo.unread_count(user_id)
    await ws.send_json({"type": "hello", "latest_inbox_id": latest, "unread_count": unread_count})

    ws_manager.connect(user_id, ws)
    try:
        while True:
            data = await ws.receive_json()
            if data.get("type") == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        ws_manager.disconnect(user_id, ws)
