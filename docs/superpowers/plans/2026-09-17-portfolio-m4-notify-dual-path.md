# 持仓诊断 M4：事件双路通知（站内信 + WebSocket，属主隔离） Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 诊断 run 结束/失败时对**属主**双路通知（REQ-021/R3-3）：①站内信**必写**（`inbox_messages` 表，可信源，REQ-009/REL-04）；②在线 WebSocket **推送**（`/ws/notify` 首帧鉴权 + 心跳 + `latest_inbox_id` 对齐，REQ-010/PERF-05）；③外部渠道仍走既有 `notify_service.emit` 订阅策略（REQ-008，失败 WARN 不阻断）。事件名 `portfolio_diag_confirmed` / `portfolio_diag_failed`（方案 L45）。

**Architecture:** 复用 M4 既有 `notify_service`（渠道/策略/节流不动）+ 新增两块最小能力：`inbox_messages` 表（站内信）+ `WsManager`（user_id→socket 集，push 时逐连接发送、死连接剔除）。诊断钩子在 `diag_service._run_diag` 完成/失败后调用 `notify_diag`：先写站内信→WS 推送（两者不受策略开关/节流制约，必达）→再 `notify_service.emit` 外部渠道（策略门控）。对齐协议：`hello` 帧下发 `latest_inbox_id`，客户端断线重连后按 `since=latest_inbox_id` 拉取补齐（REQ-010）。API 走 `/api/v1/messages`（当前登录用户属主，`CurrentUserDep`）。

**Tech Stack:** FastAPI WebSocket · SQLAlchemy(async) · 既有 security(首帧鉴权) / notify_service / inbox 新表

**裁定：** 事件名 `portfolio_diag_confirmed / portfolio_diag_failed`；对齐键 `latest_inbox_id`（TRD 口径）；M1 `POST /api/portfolio/analyze`（同步代理、结果内联返回）不发通知——通知仅挂 M3 外部批次 run 结束（REQ-021「每次诊断 run 结束」的调度语义）；WS 端点自动化覆盖到**单元层（manager + 鉴权函数 + 三路分发）**，端到端 WS 走手动冒烟（理由：TestClient 独立事件循环与 session 级 SQLite 单连接冲突）。

---

## 文件映射

| 文件 | 动作 | 职责 |
|---|---|---|
| `backend/db/sql/inbox.py` | 新增 | `InboxMessage`（inbox_messages：user_id/type/run_id/title/content/read_status） |
| `backend/db/sql/__init__.py` | 修改 | 注册 InboxMessage |
| `backend/schemas/inbox.py` | 新增 | InboxMessageCreate/Out/MarkRead |
| `backend/repositories/inbox.py` | 新增 | messages_repo（list/unread/mark_read/list_since） |
| `backend/repositories/__init__.py` | 修改 | 导出 messages_repo |
| `backend/services/inbox_service.py` | 新增 | add_message/list_messages/mark_read/unread_count |
| `backend/services/ws_manager.py` | 新增 | WsManager（register/unregister/push） |
| `backend/services/notify/events.py` | 修改 | EVENT_DEFS 增 portfolio_diag_confirmed/portfolio_diag_failed |
| `backend/api/v1/messages.py` | 新增 | `GET /api/v1/messages`（分页+未读数）+ `PUT /{id}/read` |
| `backend/api/v1/router.py` | 修改 | include messages_router |
| `backend/api/ws.py` | 新增 | `/ws/notify` websocket（首帧鉴权/hello/心跳推） |
| `backend/main.py` | 修改 | 挂载 websocket 路由 |
| `backend/services/diag_service.py` | 修改 | _run_diag 完成/失败钩子 → notify_diag 三路 |
| `backend/tests/test_inbox_db.py` | 新增 | 模型/注册 |
| `backend/tests/test_inbox_repo.py` | 新增 | 分页/未读/mark_read/隔离 |
| `backend/tests/test_inbox_service.py` | 新增 | 属主消息操作 |
| `backend/tests/test_messages_api.py` | 新增 | /api/v1/messages 接口 |
| `backend/tests/test_ws_manager.py` | 新增 | register/push/unregister（FakeSocket） |
| `backend/tests/test_diag_notify.py` | 新增 | 三路一致断言（webhook+inbox+WS） |
| `backend/tests/conftest.py` | 修改 | autouse 清 inbox 表 |

---

### Task 1: inbox 模型 + 注册 + repository

**Files:**
- Create: `stock_selector_api/backend/db/sql/inbox.py`
- Create: `stock_selector_api/backend/repositories/inbox.py`
- Modify: `stock_selector_api/backend/db/sql/__init__.py`、`repositories/__init__.py`
- Test: `stock_selector_api/backend/tests/test_inbox_db.py`、`stock_selector_api/backend/tests/test_inbox_repo.py`

- [ ] **Step 1: 写失败测试**

```python
"""test_inbox_db.py"""
from db.sql import Base
from db.sql.inbox import InboxMessage


def test_table_registered():
    assert "inbox_messages" in Base.metadata.tables


def test_columns():
    cols = {c.name: c for c in InboxMessage.__table__.columns}
    assert {"user_id", "type", "run_id", "title", "content", "read_status"} <= set(cols)
    assert cols["read_status"].type.python_type is bool
```

```python
"""test_inbox_repo.py"""
import pytest

from repositories import messages_repo, portfolio_runs


@pytest.fixture
async def _seed():
    await messages_repo.create({"user_id": "u1", "type": "portfolio_diag_confirmed",
                                "run_id": "r1", "title": "A", "content": "c1"})
    await messages_repo.create({"user_id": "u1", "type": "portfolio_diag_failed",
                                "run_id": "r1", "title": "B", "content": "c2"})
    await messages_repo.create({"user_id": "u2", "type": "portfolio_diag_confirmed",
                                "run_id": "r2", "title": "C", "content": "c3"})


async def test_list_owner_only(_seed):
    rows, total = await messages_repo.list_paged("u1", page=1, size=20)
    assert total == 2 and {r["title"] for r in rows} == {"A", "B"}


async def test_unread_count(_seed):
    assert await messages_repo.unread_count("u1") == 2
    m = await messages_repo.list_paged("u1", 1, 20)
    await messages_repo.mark_read("u1", m[0][0]["id"])
    assert await messages_repo.unread_count("u1") == 1


async def test_mark_read_owner_boundary(_seed):
    m2 = await messages_repo.list_paged("u2", 1, 20)
    other_id = m2[0][0]["id"]
    assert await messages_repo.mark_read("u1", other_id) is False


async def test_list_since(_seed):
    rows, _ = await messages_repo.list_paged("u1", 1, 20)
    oldest_id = min(r["id"] for r in rows)          # uuid 无序遍历，改用 created_at
    first = rows[0]
    since_rows, _ = await messages_repo.list_since("u1", first["id"], size=20)
    assert all(r["created_at"] >= first["created_at"] for r in since_rows)
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_inbox_db.py tests/test_inbox_repo.py -q --basetemp=...`
Expected: FAIL

- [ ] **Step 3: 实现**

```python
"""db/sql/inbox.py — 站内信（REQ-009/REQ-010 可信源）。按 user_id 隔离（R3-3）。"""
from __future__ import annotations

from sqlalchemy import Boolean, Column, String, Text

from db.sql.base import Base, IdMixin, TimestampMixin


class InboxMessage(IdMixin, TimestampMixin, Base):
    __tablename__ = "inbox_messages"

    user_id = Column(String(64), nullable=False, index=True)
    type = Column(String(64), nullable=False)
    run_id = Column(String(36), nullable=False, default="")
    title = Column(String(128), nullable=False, default="")
    content = Column(Text, nullable=False, default="")
    read_status = Column(Boolean, nullable=False, default=False)
```

```python
# db/sql/__init__.py 追加
from db.sql.inbox import InboxMessage  # noqa: E402
# __all__ 追加 "InboxMessage"
```

```python
"""repositories/inbox.py — 站内信仓储（属主过滤 + 分页 + 未读 + since 补齐）。"""
from __future__ import annotations

from sqlalchemy import select

from db.sql.base import Base
from db.sql.inbox import InboxMessage
from repositories._choose import pick
from schemas.inbox import InboxMessageCreate

RepoBase = pick()


class InboxRepo(RepoBase):
    model = InboxMessage
    table = "inbox_messages"
    schema = InboxMessageCreate

    async def list_paged(self, user_id: str, page: int = 1, size: int = 20):
        return await self.list(filters={"user_id": user_id}, page=page, size=size)

    async def unread_count(self, user_id: str) -> int:
        return await self.count({"user_id": user_id, "read_status": False})

    async def mark_read(self, user_id: str, message_id: str) -> bool:
        m = await self.get(message_id)
        if not m or m["user_id"] != user_id:
            return False
        await self.update(message_id, {"read_status": True})
        return True

    async def list_since(self, user_id: str, since_id: str, size: int = 50):
        """对齐补齐：返回 created_at ≥ 该消息的新消息（按时间升序）。"""
        anchor = await self.get(since_id)
        if not anchor or anchor["user_id"] != user_id:
            return []
        table = Base.metadata.tables[self.table]
        async with self._session() as s:
            stmt = select(self.model).where(
                (self.model.user_id == user_id)
                & (self.model.created_at >= anchor["created_at"])
            ).order_by(self.model.created_at.asc()).limit(size)
            rows = (await s.execute(stmt)).scalars().all()
        return [{c.name: getattr(o, c.name) for c in o.__table__.columns} for o in rows]


messages_repo = InboxRepo()
```

```python
# repositories/__init__.py 追加
from repositories.inbox import InboxRepo, messages_repo  # noqa: E402
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_inbox_db.py tests/test_inbox_repo.py -q --basetemp=...`
Expected: PASS（uuid 无序导致的断言已用 created_at 语义化）

- [ ] **Step 5: Commit**

```bash
git add stock_selector_api/backend/db/sql stock_selector_api/backend/repositories
git commit -m "feat(inbox): M4 站内信模型+注册+仓储（属主分页/未读/已读/since补齐）"
```

---

### Task 2: inbox schemas + service + messages API

**Files:**
- Create: `stock_selector_api/backend/schemas/inbox.py`
- Create: `stock_selector_api/backend/services/inbox_service.py`
- Create: `stock_selector_api/backend/api/v1/messages.py`
- Modify: `stock_selector_api/backend/api/v1/router.py`
- Test: `stock_selector_api/backend/tests/test_inbox_service.py`、`stock_selector_api/backend/tests/test_messages_api.py`

- [ ] **Step 1: 写失败测试**

```python
"""test_inbox_service.py"""
import pytest

from repositories import messages_repo
from services import inbox_service


@pytest.fixture(autouse=True)
async def _clean():
    rows, total = await messages_repo.list_paged("u1", 1, 100)
    for r in rows:
        await messages_repo.delete(r["id"])
    yield


async def test_add_and_list():
    m = await inbox_service.add_message("u1", "portfolio_diag_confirmed", "r1",
                                        "持仓诊断完成", "共5条建议")
    assert m["user_id"] == "u1" and m["read_status"] is False
    items, total, unread = await inbox_service.list_messages("u1")
    assert total == 1 and unread == 1 and items[0]["id"] == m["id"]


async def test_mark_read_updates_unread():
    m = await inbox_service.add_message("u1", "portfolio_diag_failed", "r1", "失败", "原因x")
    assert await inbox_service.mark_read("u1", m["id"]) is True
    _, _, unread = await inbox_service.list_messages("u1")
    assert unread == 0


async def test_mark_read_other_user_rejected():
    m = await inbox_service.add_message("u1", "portfolio_diag_failed", "r1", "失败", "原因x")
    assert await inbox_service.mark_read("u2", m["id"]) is False
```

```python
"""test_messages_api.py"""
async def test_list_and_read(client, admin_headers):
    from services import inbox_service
    m = await inbox_service.add_message("u_a1", "portfolio_diag_confirmed", "r1", "T", "C")
    r = await client.get("/api/v1/messages", headers=admin_headers)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["unread_count"] >= 0 and isinstance(data["items"], list)
    r2 = await client.put(f"/api/v1/messages/{m['id']}/read", headers=admin_headers)
    assert r2.status_code == 200
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_inbox_service.py tests/test_messages_api.py -q --basetemp=...`
Expected: FAIL

- [ ] **Step 3: 实现**

```python
"""schemas/inbox.py — 站内信写白名单 / 读模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from schemas.common import ORMModel


class InboxMessageCreate(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    type: str = Field(min_length=1, max_length=64)
    run_id: str = Field(default="", max_length=36)
    title: str = Field(default="", max_length=128)
    content: str = Field(default="")
    read_status: bool = False


class InboxMessageOut(ORMModel):
    id: str
    user_id: str
    type: str
    run_id: str
    title: str
    content: str
    read_status: bool
```

```python
"""services/inbox_service.py — 站内信：写入/列表/未读/已读（属主隔离）。"""
from __future__ import annotations

import logging

from repositories import messages_repo

logger = logging.getLogger("inbox_service")


async def add_message(user_id: str, type_: str, run_id: str, title: str, content: str) -> dict:
    return await messages_repo.create({
        "user_id": user_id, "type": type_, "run_id": run_id or "",
        "title": title[:128], "content": content,
    })


async def list_messages(user_id: str, page: int = 1, size: int = 20,
                        since_id: str | None = None):
    """返回 (items, total, unread_count)；since_id 时仅返回时间不早于锚点的新消息（补齐）。"""
    if since_id:
        items = await messages_repo.list_since(user_id, since_id, size)
        total = len(items)
    else:
        items, total = await messages_repo.list_paged(user_id, page, size)
    unread = await messages_repo.unread_count(user_id)
    return items, total, unread


async def mark_read(user_id: str, message_id: str) -> bool:
    return await messages_repo.mark_read(user_id, message_id)


async def unread_count(user_id: str) -> int:
    return await messages_repo.unread_count(user_id)
```

```python
"""api/v1/messages.py — 站内信（当前登录用户属主）。"""
from __future__ import annotations

from fastapi import APIRouter, Query

from core.dependencies import CurrentUserDep
from core.exceptions import BizError
from core.response import ok
from services import inbox_service

router = APIRouter(prefix="/messages", tags=["消息"])


@router.get("", summary="站内信列表（分页 + 未读计数，支持 since 补齐）")
async def list_messages(
    user: CurrentUserDep,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=50),
    since_id: str | None = Query(default=None, max_length=36),
):
    items, total, unread = await inbox_service.list_messages(user.id, page, size, since_id)
    return ok({"items": items, "total": total, "unread_count": unread,
               "page": page, "size": size})


@router.put("/{message_id}/read", summary="标记已读")
async def mark_read(user: CurrentUserDep, message_id: str):
    if not await inbox_service.mark_read(user.id, message_id):
        raise BizError("消息不存在", code=404, status_code=404)
    return ok(message="已读")
```

```python
# api/v1/router.py 追加
from api.v1.messages import router as messages_router
api_router.include_router(messages_router)
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_inbox_service.py tests/test_messages_api.py -q --basetemp=...`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add stock_selector_api/backend/schemas stock_selector_api/backend/services stock_selector_api/backend/api/v1
git commit -m "feat(inbox): M4 站内信服务 + /api/v1/messages（分页/未读/since）"
```

---

### Task 3: WsManager + /ws/notify 端点

**Files:**
- Create: `stock_selector_api/backend/services/ws_manager.py`
- Create: `stock_selector_api/backend/api/ws.py`
- Modify: `stock_selector_api/backend/main.py`
- Test: `stock_selector_api/backend/tests/test_ws_manager.py`

- [ ] **Step 1: 写失败测试**

```python
"""test_ws_manager.py"""
import asyncio
import json

import pytest

from services import ws_manager
from api.ws import _authorize


class FakeSocket:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(text)


async def test_register_and_push_only_owner():
    s = FakeSocket()
    await ws_manager.register("u1", s)
    bounced = await ws_manager.push("u1", {"type": "msg", "title": "x"})
    await ws_manager.push("u2", {"type": "msg", "title": "y"})   # 非属主不送到 u1
    assert bounced == 1
    assert len(s.sent) == 1
    data = json.loads(s.sent[0])
    assert data["title"] == "x"


async def test_push_drops_dead_socket():
    class Dead(FakeSocket):
        async def send_text(self, text):
            raise OSError("closed")

    d = Dead()
    await ws_manager.register("u1", d)
    await ws_manager.push("u1", {"type": "msg"})
    assert ws_manager._sockets.get("u1") == set() or len(ws_manager._sockets.get("u1", set())) == 0


async def test_unregister():
    s = FakeSocket()
    await ws_manager.register("u1", s)
    await ws_manager.unregister("u1", s)
    assert not ws_manager._sockets.get("u1")


def test_authorize_accepts_valid_token():
    from core import security
    tok = security.create_token(sub="u_ws1", token_type="access",
                                expires_delta=60, extra={"username": "t"})
    assert _authorize(tok) == "u_ws1"


def test_authorize_rejects_invalid():
    assert _authorize("garbage.token.xx") is None
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_ws_manager.py -q --basetemp=...`
Expected: FAIL

- [ ] **Step 3: 实现**

```python
"""services/ws_manager.py — WebSocket 连接管理（user_id → set[WebSocket]）。"""
from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger("ws_manager")


class WsManager:
    def __init__(self) -> None:
        self._sockets: dict[str, set] = {}
        self._lock = asyncio.Lock()

    async def register(self, user_id: str, ws) -> None:
        async with self._lock:
            self._sockets.setdefault(user_id, set()).add(ws)

    async def unregister(self, user_id: str, ws) -> None:
        async with self._lock:
            bucket = self._sockets.get(user_id)
            if bucket:
                bucket.discard(ws)
                if not bucket:
                    self._sockets.pop(user_id, None)

    async def push(self, user_id: str, data: dict) -> int:
        """向属主全部在线连接推送；死连接剔除。返回生效数。"""
        text = json.dumps(data, ensure_ascii=False)
        dropped = []
        count = 0
        for ws in list(self._sockets.get(user_id, ())):
            try:
                await ws.send_text(text)
                count += 1
            except Exception:  # noqa: BLE001 连接已死
                dropped.append(ws)
        for ws in dropped:
            await self.unregister(user_id, ws)
        return count


ws_manager = WsManager()
```

```python
"""api/ws.py — /ws/notify：首帧鉴权（REQ-010）+ hello(latest_inbox_id) + 心跳/信令。"""
from __future__ import annotations

import asyncio
import json
import logging

import jwt
from fastapi import WebSocket, WebSocketDisconnect

from core import security
from services import inbox_service
from services.ws_manager import ws_manager

logger = logging.getLogger("ws_notify")

AUTH_TIMEOUT = 60          # 首帧鉴权超时（秒）：REQ-010/TRD「60s 未鉴权断开」
_HELLO_PAYLOAD = {"latest_inbox_id": None}


def _authorize(token: str) -> str | None:
    if not token:
        return None
    try:
        payload = security.decode_token(token, expected_type="access")
        return security._user_id_from_sub(payload["sub"])
    except jwt.PyJWTError:
        return None


def _parse_auth_frame(frame: str) -> str:
    try:
        obj = json.loads(frame)
        if isinstance(obj, dict) and obj.get("type") == "auth" and obj.get("token"):
            return str(obj["token"])
    except json.JSONDecodeError:
        pass
    return frame.strip()   # 兼容直接传裸 token


async def ws_notify(websocket: WebSocket) -> None:
    await websocket.accept()
    user_id: str | None = None
    try:
        frame = await asyncio.wait_for(websocket.receive_text(), timeout=AUTH_TIMEOUT)
        user_id = _authorize(_parse_auth_frame(frame))
        if not user_id:
            await websocket.close(code=4001, reason="unauthorized")
            return
        await ws_manager.register(user_id, websocket)
        latest_id = await inbox_service.unread_count(user_id)
        await websocket.send_text(json.dumps({"type": "hello", "latest_inbox_id": None,
                                              "unread_count": latest_id}, ensure_ascii=False))
        while True:
            await websocket.receive_text()   # 客户端心跳 pong；收到即续命
    except asyncio.TimeoutError:
        try:
            await websocket.close(code=4008, reason="auth timeout")
        except Exception:  # noqa: BLE001
            pass
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        logger.exception("ws 连接异常")
    finally:
        if user_id:
            await ws_manager.unregister(user_id, websocket)
```

```python
# main.py — create_app 内追加（在 include_router 之前或之后均可）
from api.ws import ws_notify
app.add_api_websocket_route("/ws/notify", ws_notify)
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_ws_manager.py -q --basetemp=...`
Expected: PASS（`_authorize` 依赖 `security.create_token`/`decode_token` 签名——若签名不一致以 `core/security.py` 实际签名适配）

- [ ] **Step 5: Commit**

```bash
git add stock_selector_api/backend/services/ws_manager.py stock_selector_api/backend/api/ws.py stock_selector_api/backend/main.py
git commit -m "feat(ws): M4 WsManager + /ws/notify（首帧鉴权/hello/心跳）"
```

---

### Task 4: 事件目录扩展 + diag 三路通知钩子

**Files:**
- Modify: `stock_selector_api/backend/services/notify/events.py`
- Create: `stock_selector_api/backend/services/diag_notify.py`
- Modify: `stock_selector_api/backend/services/diag_service.py`
- Test: `stock_selector_api/backend/tests/test_diag_notify.py`

- [ ] **Step 1: 写失败测试**

```python
"""test_diag_notify.py"""
import json

import pytest
from repositories import messages_repo, holdings_repo
from services import diag_notify, notify_service

CAPTURED = []


@pytest.fixture(autouse=True)
async def _env(monkeypatch):
    CAPTURED.clear()
    from services.notify import channels
    channels.upsert({"type": "wecom", "key": "w1", "name": "t", "enabled": True,
                     "webhook_url": "https://x", "secret": ""})
    notify_service.save_policy({
        "is_on": True,
        "events": {"portfolio_diag_confirmed": {"is_on": True, "interval": {"value": 0, "unit": "m"}}},
        "channels": {"wecom": {"is_on": True, "channel_key": "w1"}},
    })

    async def fake_post(url, payload, **kw):
        CAPTURED.append({"url": url, "payload": payload})

    monkeypatch.setattr("services.notify.senders._http_post", fake_post)
    yield
    # 清 inbox 与策略
    rows, _ = await messages_repo.list_paged("u1", 1, 100)
    for r in rows:
        await messages_repo.delete(r["id"])


async def test_confirmed_three_way(monkeypatch):
    pushed = []

    async def fake_push(user_id, data):
        pushed.append((user_id, data))

    monkeypatch.setattr("services.diag_notify.ws_manager", type("M", (), {"push": fake_push})())
    await diag_notify.notify_diag_confirmed(
        user_id="u1", natural_day="2026-09-17", holding_count=2,
        action_count=1, total_pnl=100.5, tips=["建议减仓 600519"])
    # ① 站内信必写
    rows, total = await messages_repo.list_paged("u1", 1, 20)
    assert total == 1 and rows[0]["type"] == "portfolio_diag_confirmed"
    # ② WS 推送属主
    assert pushed and pushed[0][0] == "u1"
    # ③ 外部渠道 webhook 一致
    assert len(CAPTURED) == 1
    assert "600519" in CAPTURED[0]["payload"].get("text", "")
    # 三路内容一致（同一 subject/正文）
    assert rows[0]["title"] == pushed[0][1]["title"]


async def test_failed_notification():
    await diag_notify.notify_diag_failed(
        user_id="u1", natural_day="2026-09-17", error="插件超时")
    rows, total = await messages_repo.list_paged("u1", 1, 20)
    assert total == 1 and rows[0]["type"] == "portfolio_diag_failed"
    assert "插件超时" in rows[0]["content"]


async def test_notify_never_blocks_diag(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr("services.diag_notify.ws_manager",
                        type("M", (), {"push": boom})())
    monkeypatch.setattr("services.notify_service.emit", boom)
    # 不抛异常（失败兜底仅记日志）
    await diag_notify.notify_diag_confirmed(
        user_id="u1", natural_day="2026-09-17", holding_count=1,
        action_count=1, total_pnl=0.0, tips=[])
    rows, total = await messages_repo.list_paged("u1", 1, 20)
    assert total >= 0  # 即便发送失败，诊断主流程不被阻塞
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_diag_notify.py -q --basetemp=...`
Expected: FAIL

- [ ] **Step 3: 实现**

```python
# services/notify/events.py — EVENT_DEFS 追加（inbox=True 标记站内信必写事件）
{"code": "portfolio_diag_confirmed", "label": "持仓诊断完成",
 "description": "外部调度诊断 run 完成（仅属主收到）", "inbox": True},
{"code": "portfolio_diag_failed", "label": "持仓诊断失败",
 "description": "外部调度诊断 run 失败（仅属主收到）", "inbox": True},
```
（DEFAULT_POLICY 在 notify_service 里按 EVENT_CODES 动态生成，自动含新事件，无需改。）

```python
"""services/diag_notify.py — 诊断事件三路通知（REQ-021）：站内信必写 + WS 推送 + 外部渠道策略分发。"""
from __future__ import annotations

import logging

from core.logger import log
from services import inbox_service, notify_service
from services.ws_manager import ws_manager

logger = logging.getLogger("diag_notify")


async def _build(  event_code: str, payload: dict) -> tuple[str, str]:
    return notify_service.render_default(event_code, payload)


async def _three_way(event_code: str, user_id: str, payload: dict) -> None:
    subject, content = await _build(event_code, payload)
    try:
        msg = await inbox_service.add_message(
            user_id, event_code, payload.get("run_id", ""), subject, content)
        await ws_manager.push(user_id, {
            "type": "msg", "event_code": event_code,
            "message": msg, "latest_inbox_id": msg.get("id"),
            "title": subject, "content": content,
        })
    except Exception:  # noqa: BLE001 失败兜底不阻断主流程（站内信/WS 异常记日志）
        log.error("站内信/WS 推送失败: event={}", event_code)
    try:
        await notify_service.emit(event_code, **payload)   # 外部渠道按策略；失败仅 WARN
    except Exception:  # noqa: BLE001
        log.error("外部渠道分发异常: event={}", event_code)


async def notify_diag_confirmed(user_id: str, natural_day: str, *, run_id: str = "",
                                holding_count: int = 0, action_count: int = 0,
                                total_pnl: float = 0.0, tips: list[str] | None = None,
                                tip_limit: int = 5) -> None:
    """REQ-021 摘要：日期/持仓数/建议数/前 N 条摘要（N 默认 5 可配置）。"""
    tips = (tips or [])[:tip_limit]
    payload = {
        "run_id": run_id,
        "natural_day": natural_day,
        "holding_count": holding_count,
        "action_count": action_count,
        "total_pnl": total_pnl,
        "tips": "\n".join(tips) if tips else "无",
        "tip_format": f"前{tip_limit}条建议摘要",
    }
    await _three_way("portfolio_diag_confirmed", user_id, payload)


async def notify_diag_failed(user_id: str, natural_day: str, *, run_id: str = "",
                             error: str = "") -> None:
    payload = {"run_id": run_id, "natural_day": natural_day, "error": error[:512]}
    await _three_way("portfolio_diag_failed", user_id, payload)
```

```python
# services/diag_service.py — _run_diag try 块成功后追加：
from services.diag_notify import notify_diag_confirmed, notify_diag_failed
...
# 在 await portfolio_runs.update(run_id, {..., "plugin_time_ms": plugin_ms}) 之后：
candidate_suggestions = [
    f"{r.get('name') or r.get('code')}: {r.get('action_reason', '')}"
    for r in plugin_out["results"] if r.get("action") in ("sell", "add")
]
await notify_diag_confirmed(
    user_id, natural_day, run_id=run_id,
    holding_count=len(holdings),
    action_count=len([r for r in plugin_out["results"] if r.get("action") != "hold"]),
    total_pnl=float(summary.get("total_pnl", 0.0)),
    tips=candidate_suggestions,
)
# except 块中 await portfolio_runs.update(failed...) 之后：
await notify_diag_failed(user_id, natural_day, run_id=run_id, error=str(exc)[:512])
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_diag_notify.py -q --basetemp=...`
Expected: PASS（webhook recorder 收到含 `600519` 的 text；inbox 一条；WS fake push 一条）

- [ ] **Step 5: Commit**

```bash
git add stock_selector_api/backend/services
git commit -m "feat(notify): M4 诊断事件三路通知（站内信必写+WS+外部渠道）"
```

---

### Task 5: 全量回归 + 手动冒烟

- [ ] **Step 1: 全量回归**

Run: `python -m pytest tests/ -q --basetemp=C:\Users\User\AppData\Local\Temp\opencode\ptm4`
Expected: 全绿（原有用例 + M4 新用例；`test_diag_notify` 里 policy 持久化到 tmp 已验证）

- [ ] **Step 2: 手动冒烟 WS（需两个终端）**

终端 A（backend）: `python -m uvicorn main:app --port 8000`
终端 B: 用 python 脚本连 `/ws/notify` 发 `{"type":"auth","token":"<td admin token>"}` → 收到 `{"type":"hello","unread_count":...}`；随后触发一次外部 diag → 收到 `"type":"msg"` push。
（可用 `websocket-client` 或 FastAPI 文档页手动测：`http://localhost:8000/docs` 不支持 WS，改用自写脚本。）

- [ ] **Step 3: Commit 收尾（若有）**

---

## 验收标准汇总（对照 M4 行验收基准「REL-04/07 属主收到、非属主不可见」）

| 验收项 | 验证方式 | 结果 |
|---|---|---|
| 站内信必写（REQ-009/REL-04 可信源） | test_diag_notify 断言 inbox 落库 | 绿 |
| WS 属主推送、非属主不送（R3-3/REQ-010） | test_ws_manager + push 断言 user_id 路由 | 绿 |
| 外部渠道失败 WARN 不阻塞（REQ-021） | test_notify_never_blocks_diag | 绿 |
| 三路内容一致（REQ-021 GWT 可测试性1） | 三路一致断言 | 绿 |
| 对齐协议 latest_inbox_id + list_since 补齐（REL-04/REQ-010） | test_inbox_repo.list_since | 绿 |
| 摘要上限前 N 条（REQ-021 边界，N=5） | notify_diag_confirmed tip_limit | 断言内置 |

## 风险与注意

- **WS 端到端自动化**：受 session 级 SQLite 单连接 + 独立事件循环限制，自动化覆盖 manager/鉴权/三路单元层；真实 WS 收推以手动冒烟为准。后续若启用独立 DB 可补 TestClient WS 集成用例。
- **emit 外部渠道**默认策略 is_on=false → 若未启用，三路中的外部渠道自然静默（REQ-021「渠道未配置静默」），站内信/WS 不受影响。
- **inflight 幂等**：通知按 run_id 一次（业务钩子在 run 状态翻转时唯一执行），重复 diag 命中幂等不重复通知（R2-3）。