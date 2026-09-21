"""B9：站内信仓储单测（不经 HTTP，直接打 repo）。

conftest 不清理 inbox 表，各用例使用独立 user_id 避免串扰。
"""

import pytest

from repositories import inbox_repo


@pytest.mark.asyncio
async def test_create_and_list():
    uid = "ins1"
    await inbox_repo.create({"user_id": uid, "event_code": "diag_done", "title": "诊断完成", "content": "请查看"})
    await inbox_repo.create({"user_id": uid, "event_code": "sel_done", "title": "选股完成", "content": "请查看"})
    items, total, unread = await inbox_repo.list_for(uid, page=1, size=10)
    assert total == 2 and unread == 2
    assert {i["title"] for i in items} == {"诊断完成", "选股完成"}


@pytest.mark.asyncio
async def test_mark_read_and_unread_filter():
    uid = "ins2"
    msg = await inbox_repo.create({"user_id": uid, "event_code": "e", "title": "t1", "content": "c"})
    await inbox_repo.create({"user_id": uid, "event_code": "e", "title": "t2", "content": "c"})
    assert await inbox_repo.unread_count(uid) == 2
    ok = await inbox_repo.mark_read(msg["id"], uid)
    assert ok is True
    assert await inbox_repo.unread_count(uid) == 1
    items, _, _ = await inbox_repo.list_for(uid, page=1, size=10, unread_only=True)
    assert [i["title"] for i in items] == ["t2"]
    # 他人不可标记
    assert await inbox_repo.mark_read(msg["id"], "other-uid") is False
    await inbox_repo.mark_all_read(uid)
    assert await inbox_repo.unread_count(uid) == 0
    items, _, _ = await inbox_repo.list_for(uid, page=1, size=10)
    assert all(i["read_at"] for i in items)


@pytest.mark.asyncio
async def test_latest_and_after():
    uid = "ins3"
    a = await inbox_repo.create({"user_id": uid, "event_code": "e", "title": "a", "content": "c"})
    assert await inbox_repo.latest_id(uid) == a["id"]
    b = await inbox_repo.create({"user_id": uid, "event_code": "e", "title": "b", "content": "c"})
    assert await inbox_repo.latest_id(uid) == b["id"]
    # 首帧重置：since_id=最新 → 空
    items, total, _ = await inbox_repo.list_for(uid, page=1, size=10, since_id=b["id"])
    assert total == 0
    # since_id=首条 → 仅新消息（时间序，非 id 序）
    items, total, _ = await inbox_repo.list_for(uid, page=1, size=10, since_id=a["id"])
    assert total == 1 and items[0]["title"] == "b"
