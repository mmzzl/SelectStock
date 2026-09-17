"""通知组件专项测试：渠道配置（加密落盘/脱敏/CRUD）、订阅策略读写、测试发送、事件分发与节流、三处触发钩子、权限门槛。"""

import json

import pytest

from config import settings


async def _admin_headers(client):
    from .helpers import login_with_captcha

    r = await login_with_captcha(client, "admin", "admin123")
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['data']['access_token']}"}


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
    base = (await client.get("/api/v1/notify/policy", headers=admin_headers)).json()["data"]
    base["is_on"] = True
    base["events"]["custom"]["is_on"] = True
    base["channels"]["wecom"]["is_on"] = True
    base["channels"]["wecom"]["channel_key"] = "up"
    await client.put("/api/v1/notify/policy", json={"policy": base}, headers=admin_headers)

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
# 策略读写
# ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_policy_roundtrip(client, admin_headers):
    r = await client.get("/api/v1/notify/policy", headers=admin_headers)
    assert r.status_code == 200
    base = r.json()["data"]
    assert base["is_on"] is False and set(base["events"]) == {"account_locked", "login_failure", "task_failed", "custom"}

    base["is_on"] = True
    base["events"]["task_failed"]["is_on"] = True
    base["channels"]["wecom"]["channel_key"] = "wc"
    r = await client.put("/api/v1/notify/policy", json={"policy": base}, headers=admin_headers)
    assert r.status_code == 200, r.text
    saved = r.json()["data"]
    assert saved["is_on"] is True
    assert saved["events"]["task_failed"]["is_on"] is True
    assert saved["channels"]["wecom"]["channel_key"] == "wc"


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
    await client.post("/api/v1/notify/channels", json={
        "type": "wecom", "key": "wc", "name": "运维群",
        "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc", "enabled": True,
    }, headers=admin_headers)
    base = (await client.get("/api/v1/notify/policy", headers=admin_headers)).json()["data"]
    base["is_on"] = True
    base["events"]["account_locked"]["is_on"] = True
    base["channels"]["wecom"]["is_on"] = True
    base["channels"]["wecom"]["channel_key"] = "wc"
    await client.put("/api/v1/notify/policy", json={"policy": base}, headers=admin_headers)

    from services import notify_service

    await notify_service.emit("account_locked", username="admin", ip="1.2.3.4")
    assert recorder, "应实际调用发送器"
    assert recorder[0]["payload"]["text"]["content"] and "账号/IP 被锁定" in recorder[0]["payload"]["text"]["content"]


@pytest.mark.asyncio
async def test_emit_throttled_by_interval(client, admin_headers, recorder):
    await client.post("/api/v1/notify/channels", json={
        "type": "wecom", "key": "wc", "name": "运维群",
        "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc", "enabled": True,
    }, headers=admin_headers)
    base = (await client.get("/api/v1/notify/policy", headers=admin_headers)).json()["data"]
    base["is_on"] = True
    base["events"]["login_failure"]["is_on"] = True
    base["events"]["login_failure"]["interval"] = {"value": 5, "unit": "m"}
    base["channels"]["wecom"]["is_on"] = True
    base["channels"]["wecom"]["channel_key"] = "wc"
    await client.put("/api/v1/notify/policy", json={"policy": base}, headers=admin_headers)

    from services import notify_service

    await notify_service.emit("login_failure", username="u", ip="x")
    await notify_service.emit("login_failure", username="u2", ip="y")
    assert len(recorder) == 1, "5 分钟间隔内第二次应被节流"

    # 回调 state 的最后发送时间 → 可再次发送
    notify_service._save_state({"login_failure": {"last": 1}})
    await notify_service.emit("login_failure", username="u3", ip="z")
    assert len(recorder) == 2


# ---------------------------------------------------------------
# 触发钩子：登录失败 / 账号锁定 / 定时任务失败
# ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_hook_login_failure_emits(client, admin_headers, recorder, monkeypatch):
    await client.post("/api/v1/notify/channels", json={
        "type": "wecom", "key": "wc", "name": "运维群",
        "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc", "enabled": True,
    }, headers=admin_headers)
    base = (await client.get("/api/v1/notify/policy", headers=admin_headers)).json()["data"]
    base["is_on"] = True
    base["events"]["login_failure"]["is_on"] = True
    base["channels"]["wecom"]["is_on"] = True
    base["channels"]["wecom"]["channel_key"] = "wc"
    await client.put("/api/v1/notify/policy", json={"policy": base}, headers=admin_headers)

    monkeypatch.setattr(settings, "captcha_enabled", False)
    r = await client.post("/api/v1/auth/login", json={
        "username": "admin", "password": "wrongpass", "captcha_id": None, "captcha_code": None,
    })
    assert r.status_code == 401
    assert any("登录失败" in c["payload"]["text"]["content"] for c in recorder), recorder


@pytest.mark.asyncio
async def test_hook_account_locked_emits(client, admin_headers, recorder, monkeypatch):
    await client.post("/api/v1/notify/channels", json={
        "type": "wecom", "key": "wc", "name": "运维群",
        "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc", "enabled": True,
    }, headers=admin_headers)
    base = (await client.get("/api/v1/notify/policy", headers=admin_headers)).json()["data"]
    base["is_on"] = True
    base["events"]["account_locked"]["is_on"] = True
    base["channels"]["wecom"]["is_on"] = True
    base["channels"]["wecom"]["channel_key"] = "wc"
    await client.put("/api/v1/notify/policy", json={"policy": base}, headers=admin_headers)

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
    r = await client.post("/api/v1/notify/channels", json={
        "type": "wecom", "key": "x", "name": "x", "webhook_url": "http://x", "enabled": True,
    }, headers=h)
    assert r.status_code == 403, r.text
