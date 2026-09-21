"""B11：WS 通知测试。

conftest 的 client 是 httpx ASGITransport，不支持 websocket_connect，
故 handler 级测试用 Duck Typed FakeWS 直接驱动 ws_notify（脚本化 receive_json）。
"""

import pytest
from fastapi import WebSocketDisconnect

from repositories import inbox_repo
from services import inbox_notify
from services.ws_manager import WsManager, ws_manager


class FakeWS:
    """脚本化入站帧；耗尽后模拟客户端断开。"""

    def __init__(self, frames: list[dict]):
        self._frames = list(frames)
        self.sent: list[dict] = []
        self.closed_code: int | None = None
        self.accepted = False

    async def accept(self):
        self.accepted = True

    async def receive_json(self):
        if not self._frames:
            raise WebSocketDisconnect(code=1000)
        return self._frames.pop(0)

    async def send_json(self, obj):
        self.sent.append(obj)

    async def close(self, code: int = 1000):
        self.closed_code = code


async def _admin_id() -> str:
    from repositories import users

    user = await users.get_by_username("admin")
    return str(user["id"])


async def _reset_inbox(user_id: str):
    rows = await inbox_repo.list_for(user_id, page=1, size=500)
    for m in rows[0]:
        await inbox_repo._delete_from("inbox_messages", {"id": m["id"]})


async def test_push_only_to_target_user():
    received = []

    class Sink:
        async def send_json(self, obj):
            received.append(obj)

    mgr = WsManager()
    f1, f2 = Sink(), Sink()
    mgr.connect("w1", f1)
    mgr.connect("w2", f2)
    n = await mgr.push("w1", {"type": "msg", "message": {"id": "m1"}})
    assert n == 1
    assert [m["message"]["id"] for m in received] == ["m1"]
    mgr.disconnect("w1", f1)
    assert await mgr.push("w1", {"type": "msg", "message": {"id": "m2"}}) == 0


async def test_push_drops_broken_connection_only():
    class Broken:
        async def send_json(self, obj):
            raise RuntimeError("gone")

    class Good:
        def __init__(self): self.n = 0
        async def send_json(self, obj): self.n += 1

    mgr = WsManager()
    b, g = Broken(), Good()
    mgr.connect("u", b)
    mgr.connect("u", g)
    n = await mgr.push("u", {"type": "msg"})
    assert n == 1 and g.n == 1
    assert mgr._conns["u"] == {g}


async def test_ws_bad_first_frame_closes_4401():
    from api.ws_notify import ws_notify

    ws = FakeWS([{"type": "other"}])
    await ws_notify(ws)
    assert ws.closed_code == 4401
    assert ws.sent == []


async def test_ws_bad_token_closes_4401():
    from api.ws_notify import ws_notify

    ws = FakeWS([{"type": "auth", "token": "not-a-jwt"}])
    await ws_notify(ws)
    assert ws.closed_code == 4401


async def test_ws_hello_ping_pong(admin_token):
    from api.ws_notify import ws_notify

    uid = await _admin_id()
    await _reset_inbox(uid)
    msg = await inbox_notify.notify_user(uid, "selection_completed", "T", "C")

    ws = FakeWS([
        {"type": "auth", "token": admin_token},
        {"type": "ping"},
    ])
    await ws_notify(ws)
    hello = ws.sent[0]
    assert hello["type"] == "hello"
    assert hello["latest_inbox_id"] == msg["id"]
    assert hello["unread_count"] == 1
    assert ws.sent[1] == {"type": "pong"}
    # 帧耗尽 → WebSocketDisconnect → 正常清理，不再挂在线
    assert ws.closed_code is None
    assert len(ws_manager._conns.get(uid, ())) == 0


async def test_hello_newer_message_is_latest(admin_token):
    """hello.latest_inbox_id 取时间序最新（since_id 重置基线语义）。"""
    from api.ws_notify import ws_notify

    uid = await _admin_id()
    await _reset_inbox(uid)
    await inbox_notify.notify_user(uid, "e", "old", "c")
    newest = await inbox_notify.notify_user(uid, "e", "new", "c")

    ws = FakeWS([{"type": "auth", "token": admin_token}])
    await ws_notify(ws)
    assert ws.sent[0]["latest_inbox_id"] == newest["id"]
    assert ws.sent[0]["unread_count"] == 2
