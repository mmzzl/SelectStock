"""B10：站内信 API 流程测试。

conftest 不清理 inbox 表，且本文件用精确 total/unread 计数断言，
故在本模块内做前后清场，避免跨用例/跨文件（B13 起会 notify admin）污染。
"""

import pytest

from repositories import inbox_repo


@pytest.fixture(autouse=True)
async def _clean_inbox():
    await inbox_repo._delete_from("inbox_messages", {})
    yield
    await inbox_repo._delete_from("inbox_messages", {})


from services import inbox_notify  # noqa: E402（依赖清场 fixture 生效顺序）


async def test_messages_api_flow(client, admin_headers):
    me = (await client.get("/api/v1/auth/me", headers=admin_headers)).json()["data"]
    await inbox_notify.notify_user(me["id"], "selection_completed", "选股完成", "命中 3 只")
    await inbox_notify.notify_user(me["id"], "selection_completed", "选股完成", "命中 5 只")

    r = await client.get("/api/v1/messages", headers=admin_headers)
    data = r.json()["data"]
    assert data["total"] == 2 and data["unread_count"] == 2
    assert all(i["read_status"] for i in data["items"])

    mid = data["items"][0]["id"]
    r2 = await client.put(f"/api/v1/messages/{mid}/read", headers=admin_headers)
    assert r2.status_code == 200
    r3 = await client.get("/api/v1/messages?unread_only=true", headers=admin_headers)
    assert r3.json()["data"]["unread_count"] == 1

    await client.put("/api/v1/messages/read_all", headers=admin_headers)
    r4 = await client.get("/api/v1/messages", headers=admin_headers)
    assert r4.json()["data"]["unread_count"] == 0


async def test_message_not_found_for_other_owner(client, admin_headers):
    # 属主隔离（R3-1）：他人的消息标记已读 → 404
    msg = await inbox_notify.notify_user("someone-else", "e", "t", "c")
    r = await client.put(f"/api/v1/messages/{msg['id']}/read", headers=admin_headers)
    assert r.status_code == 404
    # 列表也看不到他人消息
    r2 = await client.get("/api/v1/messages", headers=admin_headers)
    assert r2.json()["data"]["total"] == 0


async def test_messages_requires_auth(client):
    r = await client.get("/api/v1/messages")
    assert r.status_code in (401, 403)
