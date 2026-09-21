"""内存 WS 连接管理器：按 user_id 分组；push 仅投递在线连接。"""
from __future__ import annotations

from collections import defaultdict


class WsManager:
    def __init__(self) -> None:
        self._conns: dict[str, set] = defaultdict(set)

    def connect(self, user_id: str, ws) -> None:
        self._conns[user_id].add(ws)

    def disconnect(self, user_id: str, ws) -> None:
        self._conns.get(user_id, set()).discard(ws)

    async def push(self, user_id: str, payload: dict) -> int:
        n = 0
        for ws in list(self._conns.get(user_id, ())):
            try:
                await ws.send_json(payload)
                n += 1
            except Exception:   # noqa: BLE001 单个连接失败不影响其余
                self.disconnect(user_id, ws)
        return n


ws_manager = WsManager()
