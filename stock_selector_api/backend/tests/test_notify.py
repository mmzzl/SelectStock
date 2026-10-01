"""通知组件专项测试：渠道配置（加密落盘/脱敏/CRUD）、订阅策略 CRUD、测试发送、事件分发与节流、三处触发钩子、权限门槛。"""

import json

import pytest

from config import settings


async def _admin_headers(client):
    from .helpers import login_with_captcha

    r = await login_with_captcha(client, "admin", "admin123")
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['data']['access_token']}"}


async def _add_policy(client, headers, *, name="测试策略", event="custom",
                      interval=None, channel_type="wecom", channel_key="wc", is_on=True):
    """建一条订阅策略：只订阅一个事件并绑定一个渠道实例（其余事件/渠道落成关闭占位）。"""
    r = await client.post("/api/v1/notify/policies", json={
        "name": name,
        "is_on": is_on,
        "events": {event: {"is_on": True, "interval": interval or {"value": 0, "unit": "m"}}},
        "channels": {channel_type: {"is_on": True, "channel_key": channel_key}},
    }, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["data"]


async def _add_wecom(client, headers, key, name="运维群"):
    r = await client.post("/api/v1/notify/channels", json={
        "type": "wecom", "key": key, "name": name,
        "webhook_url": f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}", "enabled": True,
    }, headers=headers)
    assert r.status_code == 200, r.text


@pytest.fixture
async def recorder(monkeypatch):
    """替换 senders._http_post 为记录器，避免真实外呼。"""
    calls = []

    async def fake_post(url, payload):
        calls.append({"url": url, "payload": payload})

    monkeypatch.setattr("services.notify.senders._http_post", fake_post)
    return calls


# ---------------------------------------------------------------
# 渠道配置：加密落盘 + 脱敏回显 + CRUD
# ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_channel_password_encrypted_and_masked(client, admin_headers):
    r = await client.post("/api/v1/notify/channels", json={
        "type": "email", "key": "smtp_test", "name": "测试邮箱",
        "host": "smtp.corp.com", "port": 465, "use_ssl": True,
        "username": "noreply@corp.com", "password": "s3cret-pass", "enabled": True,
    }, headers=admin_headers)
    assert r.status_code == 200, r.text

    # 落盘必然密文
    from core import security
    from services.notify import channels as chs

    entry = chs._load_entry("smtp_test")
    assert entry["password"] != "s3cret-pass"
    assert security.decrypt_secret(entry["password"]) == "s3cret-pass"

    # 对外视图只暴露"是否已配置"
    listed = chs.list_all("email")
    item = next(c for c in listed if c["key"] == "smtp_test")
    assert item["password"] is True

    # 编辑不填密码 => 保留原值
    r = await client.put("/api/v1/notify/channels/smtp_test", json={
        "type": "email", "key": "smtp_test", "name": "改名", "host": "smtp.corp.com",
        "port": 465, "use_ssl": True, "username": "noreply@corp.com",
        "password": "", "from_email": "", "from_name": "", "enabled": True,
    }, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert security.decrypt_secret(chs._load_entry("smtp_test")["password"]) == "s3cret-pass"

    # 删除
    r = await client.delete("/api/v1/notify/channels/smtp_test", headers=admin_headers)
    assert r.status_code == 200
    assert chs.get_by_key("smtp_test") is None


@pytest.mark.asyncio
async def test_channel_wecom_roundtrip(client, admin_headers):
    r = await client.post("/api/v1/notify/channels", json={
        "type": "wecom", "key": "wc", "name": "运维群",
        "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc", "enabled": True,
    }, headers=admin_headers)
    assert r.status_code == 200, r.text
    r = await client.get("/api/v1/notify/channels", headers=admin_headers)
    items = r.json()["data"]
    assert any(c["key"] == "wc" and c["type"] == "wecom" for c in items)


@pytest.mark.asyncio
async def test_handwritten_channel_entry_gets_type(client, admin_headers):
    """手改的渠道 JSON 常漏 type：分桶键补齐后，列表/按类型查询/策略页下拉才都能用。"""
    from pathlib import Path

    from services.notify import channels as chs

    Path(chs._file()).write_text(json.dumps({
        "wecom": [{"key": "hand", "name": "手写群", "webhook_url": "http://h"}],
    }), encoding="utf-8")

    listed = chs.list_all("wecom")
    assert [c["key"] for c in listed] == ["hand"]
    assert listed[0]["type"] == "wecom"
    assert chs.get_by_key("hand", "wecom") is not None


@pytest.mark.asyncio
async def test_channel_update_persists_and_rotates_secret(client, admin_headers):
    """PUT 更新必须真正落盘：改名/换 webhook/禁用/换密码生效，新密码重新加密，跨类型同 key 拒绝。"""
    from core import security
    from services.notify import channels as chs

    await client.post("/api/v1/notify/channels", json={
        "type": "wecom", "key": "up", "name": "旧名",
        "webhook_url": "http://old", "enabled": True,
    }, headers=admin_headers)
    r = await client.put("/api/v1/notify/channels/up", json={
        "type": "wecom", "key": "up", "name": "新名",
        "webhook_url": "http://new", "enabled": False,
    }, headers=admin_headers)
    assert r.status_code == 200, r.text
    entry = chs._load_entry("up")
    assert entry["name"] == "新名" and entry["webhook_url"] == "http://new"
    assert entry["enabled"] is False

    # 换密码 → 旧密文被新密文覆盖
    await client.post("/api/v1/notify/channels", json={
        "type": "email", "key": "up2", "name": "邮箱", "host": "smtp.c.com",
        "port": 465, "use_ssl": True, "username": "a@c.com", "password": "old-pass", "enabled": True,
    }, headers=admin_headers)
    r = await client.put("/api/v1/notify/channels/up2", json={
        "type": "email", "key": "up2", "name": "邮箱", "host": "smtp.c.com",
        "port": 465, "use_ssl": True, "username": "a@c.com", "password": "new-pass",
    }, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert security.decrypt_secret(chs._load_entry("up2")["password"]) == "new-pass"

    # 跨类型同 key → 拒绝（key 全局唯一，避免订阅策略引用歧义）
    r = await client.post("/api/v1/notify/channels", json={
        "type": "dingtalk", "key": "up", "name": "钉钉", "webhook_url": "http://d", "enabled": True,
    }, headers=admin_headers)
    assert r.status_code == 400 and "key" in r.json()["message"], r.text

    # 已禁用的渠道在发送侧被跳过（emit 的 enabled 守卫可用）
    await _add_policy(client, admin_headers, event="custom", channel_key="up")

    from services import notify_service

    with_records = []
    original = notify_service.senders._http_post

    async def fake_post(url, payload):
        with_records.append({"url": url, "payload": payload})

    notify_service.senders._http_post = fake_post
    try:
        await notify_service.emit("custom", x=1)
    finally:
        notify_service.senders._http_post = original
    assert not with_records, "禁用渠道不应收到通知"

    # 删除不留孤儿
    await client.delete("/api/v1/notify/channels/up", headers=admin_headers)
    assert chs.get_by_key("up") is None


# ---------------------------------------------------------------
# 策略 CRUD
# ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_policy_crud_list_update_delete(client, admin_headers):
    """可新增多条、列表按插入序返回、单条可改可删；未订阅的事件落成关闭占位。"""
    p1 = await _add_policy(client, admin_headers, name="安全事件", event="login_failure")
    p2 = await _add_policy(client, admin_headers, name="任务失败", event="task_failed",
                           channel_type="dingtalk", channel_key="dt")
    assert p1["id"] != p2["id"]
    assert set(p1["events"]) == {"account_locked", "login_failure", "task_failed", "custom"}
    assert p1["events"]["task_failed"]["is_on"] is False

    listed = (await client.get("/api/v1/notify/policies", headers=admin_headers)).json()["data"]
    assert [p["name"] for p in listed] == ["安全事件", "任务失败"]

    detail = (await client.get(f"/api/v1/notify/policies/{p1['id']}", headers=admin_headers)).json()["data"]
    assert detail["channels"]["wecom"]["channel_key"] == "wc"

    r = await client.put(f"/api/v1/notify/policies/{p1['id']}", json={
        "name": "安全事件-改名", "is_on": False,
        "events": {"login_failure": {"is_on": True, "interval": {"value": 2, "unit": "h"}}},
        "channels": {"wecom": {"is_on": True, "channel_key": "wc"}},
    }, headers=admin_headers)
    assert r.status_code == 200, r.text
    saved = r.json()["data"]
    assert saved["name"] == "安全事件-改名" and saved["is_on"] is False
    assert saved["events"]["login_failure"]["interval"] == {"value": 2, "unit": "h"}

    r = await client.delete(f"/api/v1/notify/policies/{p2['id']}", headers=admin_headers)
    assert r.status_code == 200, r.text
    listed = (await client.get("/api/v1/notify/policies", headers=admin_headers)).json()["data"]
    assert [p["id"] for p in listed] == [p1["id"]]


@pytest.mark.asyncio
async def test_policy_rejects_dirty_payload(client, admin_headers):
    """空名 / 未知事件码 / 未知渠道类型 / 不存在的 id 都要拒——静默存下半截配置最难查。"""
    r = await client.post("/api/v1/notify/policies", json={
        "name": "", "events": {}, "channels": {},
    }, headers=admin_headers)
    assert r.status_code == 422, r.text

    r = await client.post("/api/v1/notify/policies", json={
        "name": "x", "events": {"no_such_event": {"is_on": True}}, "channels": {},
    }, headers=admin_headers)
    assert r.status_code == 400 and "no_such_event" in r.json()["message"], r.text

    r = await client.post("/api/v1/notify/policies", json={
        "name": "x", "events": {}, "channels": {"sms": {"is_on": True}},
    }, headers=admin_headers)
    assert r.status_code == 400 and "sms" in r.json()["message"], r.text

    r = await client.put("/api/v1/notify/policies/nonexist", json={
        "name": "x", "events": {}, "channels": {},
    }, headers=admin_headers)
    assert r.status_code == 400 and "不存在" in r.json()["message"], r.text

    r = await client.delete("/api/v1/notify/policies/nonexist", headers=admin_headers)
    assert r.status_code == 400, r.text


# ---------------------------------------------------------------
# 测试发送 + 邮件收件人必填
# ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_email_requires_recipient(client, admin_headers):
    await client.post("/api/v1/notify/channels", json={
        "type": "email", "key": "em", "name": "邮箱",
        "host": "smtp.corp.com", "port": 25, "use_ssl": False, "username": "u@c.com", "enabled": True,
    }, headers=admin_headers)
    r = await client.post("/api/v1/notify/channels/em/test", json={"text": "hi"}, headers=admin_headers)
    assert r.status_code == 400 and "收件人" in r.json()["message"], r.text


@pytest.mark.asyncio
async def test_test_send_wecom_via_recorder(client, admin_headers, recorder):
    await client.post("/api/v1/notify/channels", json={
        "type": "wecom", "key": "wc", "name": "运维群",
        "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc", "enabled": True,
    }, headers=admin_headers)
    r = await client.post("/api/v1/notify/channels/wc/test", json={"text": "你好"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert recorder and recorder[0]["payload"] == {"msgtype": "text", "text": {"content": "你好"}}


# ---------------------------------------------------------------
# 事件分发与节流
# ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_emit_dispatches_to_enabled_channels(client, admin_headers, recorder):
    await _add_wecom(client, admin_headers, "wc")
    await _add_policy(client, admin_headers, event="account_locked")

    from services import notify_service

    await notify_service.emit("account_locked", username="admin", ip="1.2.3.4")
    assert recorder, "应实际调用发送器"
    assert recorder[0]["payload"]["text"]["content"] and "账号/IP 被锁定" in recorder[0]["payload"]["text"]["content"]


@pytest.mark.asyncio
async def test_disabled_policy_does_not_emit(client, admin_headers, recorder):
    """策略自身停用 → 一条都不发。"""
    await _add_wecom(client, admin_headers, "wc")
    await _add_policy(client, admin_headers, event="account_locked", is_on=False)

    from services import notify_service

    await notify_service.emit("account_locked", username="admin", ip="1.2.3.4")
    assert not recorder


@pytest.mark.asyncio
async def test_emit_throttled_by_interval(client, admin_headers, recorder):
    await _add_wecom(client, admin_headers, "wc")
    await _add_policy(client, admin_headers, event="login_failure",
                      interval={"value": 5, "unit": "m"})

    from services import notify_service

    await notify_service.emit("login_failure", username="u", ip="x")
    await notify_service.emit("login_failure", username="u2", ip="y")
    assert len(recorder) == 1, "5 分钟间隔内第二次应被节流"

    # 清空节流记账 → 可再次发送
    notify_service._save_state({})
    await notify_service.emit("login_failure", username="u3", ip="z")
    assert len(recorder) == 2


@pytest.mark.asyncio
async def test_two_policies_same_event_send_independently(client, admin_headers, recorder):
    """两条策略订阅同一事件、各自绑一个渠道：都必须发出。

    节流若仍按事件码记账，第二条会被第一条的发送时间压掉，这里就会只剩 1 条。
    """
    from services import notify_service

    await _add_wecom(client, admin_headers, "wc", name="运维群")
    await _add_wecom(client, admin_headers, "wc2", name="业务群")
    await _add_policy(client, admin_headers, name="运维", event="login_failure",
                      interval={"value": 5, "unit": "m"}, channel_key="wc")
    await _add_policy(client, admin_headers, name="业务", event="login_failure",
                      interval={"value": 5, "unit": "m"}, channel_key="wc2")

    await notify_service.emit("login_failure", username="u", ip="x")
    urls = {c["url"] for c in recorder}
    assert len(recorder) == 2, recorder
    assert len(urls) == 2, "两条策略应各走自己的 webhook"

    # 各自都被自己的间隔节流，且互不影响
    await notify_service.emit("login_failure", username="u2", ip="y")
    assert len(recorder) == 2


# ---------------------------------------------------------------
# 触发钩子：登录失败 / 账号锁定 / 定时任务失败
# ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_hook_login_failure_emits(client, admin_headers, recorder, monkeypatch):
    await _add_wecom(client, admin_headers, "wc")
    await _add_policy(client, admin_headers, event="login_failure")

    monkeypatch.setattr(settings, "captcha_enabled", False)
    r = await client.post("/api/v1/auth/login", json={
        "username": "admin", "password": "wrongpass", "captcha_id": None, "captcha_code": None,
    })
    assert r.status_code == 401
    assert any("登录失败" in c["payload"]["text"]["content"] for c in recorder), recorder


@pytest.mark.asyncio
async def test_hook_account_locked_emits(client, admin_headers, recorder, monkeypatch):
    await _add_wecom(client, admin_headers, "wc")
    await _add_policy(client, admin_headers, event="account_locked")

    monkeypatch.setattr(settings, "captcha_enabled", False)
    monkeypatch.setattr(settings, "login_max_failures", 1)
    monkeypatch.setattr(settings, "captcha_max_failures", 100)
    monkeypatch.setattr(settings, "lock_seconds", 300)

    r = await client.post("/api/v1/auth/login", json={
        "username": "admin", "password": "bad", "captcha_id": None, "captcha_code": None,
    })
    assert r.status_code in (401, 429)
    joined = "\n".join(c["payload"]["text"]["content"] for c in recorder)
    assert "账号/IP 被锁定" in joined, joined


@pytest.mark.asyncio
async def test_hook_task_failed_emits(recorder, monkeypatch):
    """定时任务抛异常 → 订阅策略 task_failed 事件被触发。"""
    from scheduler import scheduler_ as sch

    calls = []

    async def fake_emit(code, **kwargs):
        calls.append((code, kwargs))

    monkeypatch.setattr("services.notify_service.emit", fake_emit)

    async def boom(item):
        raise RuntimeError("boom")

    class FakeItem:
        section = "test_task"
        title = None

    await sch._task_wrapper(boom, FakeItem())
    assert calls and calls[0][0] == "task_failed", calls
    assert "boom" in str(calls[0][1].get("error")), calls


# ---------------------------------------------------------------
# 权限门槛：readonly 无通知权限
# ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_readonly_denied_notify(client, admin_headers):
    roles = (await client.get("/api/v1/sys/roles", headers=admin_headers)).json()["data"]
    readonly = next(r for r in roles if r["code"] == "readonly")
    r = await client.post("/api/v1/sys/users", json={
        "username": "nview", "password": "Viewer123", "role_ids": [readonly["id"]],
    }, headers=admin_headers)
    assert r.status_code == 200, r.text

    from .helpers import login_with_captcha

    r = await login_with_captcha(client, "nview", "Viewer123")
    h = {"Authorization": f"Bearer {r.json()['data']['access_token']}"}
    r = await client.get("/api/v1/notify/events", headers=h)
    assert r.status_code == 403, r.text
    r = await client.get("/api/v1/notify/policies", headers=h)
    assert r.status_code == 403, r.text
    r = await client.post("/api/v1/notify/policies", json={
        "name": "越权", "events": {}, "channels": {},
    }, headers=h)
    assert r.status_code == 403, r.text
    r = await client.post("/api/v1/notify/channels", json={
        "type": "wecom", "key": "x", "name": "x", "webhook_url": "http://x", "enabled": True,
    }, headers=h)
    assert r.status_code == 403, r.text
