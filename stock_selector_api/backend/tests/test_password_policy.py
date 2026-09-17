"""密码策略专项测试：强度校验、历史防重用、自助改密、管理重置、定期强制改密（jwt/session 都验证）。

注意：密码改动用例一律用一次性测试用户，避免改到 admin 密码污染其他用例。
"""

import pytest
from datetime import datetime, timedelta, timezone

from config import settings


async def _admin_headers(client):
    from .helpers import login_with_captcha

    r = await login_with_captcha(client, "admin", "admin123")
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['data']['access_token']}"}


async def _create_user(client, admin_headers, username, password, role_ids=None, display_name="x"):
    body = {"username": username, "password": password, "display_name": display_name}
    if role_ids is not None:
        body["role_ids"] = role_ids
    r = await client.post("/api/v1/sys/users", json=body, headers=admin_headers)
    assert r.status_code == 200, r.text
    return r.json()["data"]["id"]


# ---------------------------------------------------------------
# 强度校验（建号时统一拦截，配置驱动）
# ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_password_strength_enforced_on_create(client, admin_headers):
    cases = [
        ("w1", "ab", "长度"),
        ("w2", "abcdefg1", "大写"),
        ("w3", "ABCDEFG1", "小写"),
        ("w4", "Abcdefgh", "数字"),
    ]
    for username, pw, keyword in cases:
        r = await client.post("/api/v1/sys/users", json={
            "username": username, "password": pw, "display_name": "x",
        }, headers=admin_headers)
        assert r.status_code == 400 and keyword in r.json()["message"], (username, pw, r.text)

    r = await client.post("/api/v1/sys/users", json={
        "username": "w5", "password": "Abcd1234", "display_name": "x",
    }, headers=admin_headers)
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------
# 自助改密：验旧密 + 强度 + 改后新密生效
# ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_change_own_password(client, admin_headers):
    await _create_user(client, admin_headers, "selfpw", "Startpw1")

    from .helpers import login_with_captcha

    r = await login_with_captcha(client, "selfpw", "Startpw1")
    h = {"Authorization": f"Bearer {r.json()['data']['access_token']}"}

    # 旧密码错 → 400
    r = await client.post("/api/v1/auth/change-password", json={
        "old_password": "wrongpass", "new_password": "NewPass123",
    }, headers=h)
    assert r.status_code == 400 and "旧密码" in r.json()["message"], r.text

    # 弱新密码 → 400（缺数字）
    r = await client.post("/api/v1/auth/change-password", json={
        "old_password": "Startpw1", "new_password": "NewPassxx",
    }, headers=h)
    assert r.status_code == 400, r.text

    # 改成与当前相同 → 400
    r = await client.post("/api/v1/auth/change-password", json={
        "old_password": "Startpw1", "new_password": "Startpw1",
    }, headers=h)
    assert r.status_code == 400, r.text

    # 成功改密
    r = await client.post("/api/v1/auth/change-password", json={
        "old_password": "Startpw1", "new_password": "NewPass123",
    }, headers=h)
    assert r.status_code == 200, r.text

    # 新密码可登录、旧密码失效
    r = await login_with_captcha(client, "selfpw", "NewPass123")
    assert r.status_code == 200, r.text
    r = await login_with_captcha(client, "selfpw", "Startpw1")
    assert r.status_code != 200


# ---------------------------------------------------------------
# 历史防重用：最近 N 个旧密码拒绝改回
# ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_password_history_reuse_blocked(client, admin_headers, monkeypatch):
    monkeypatch.setattr(settings, "password_history_count", 2)
    await _create_user(client, admin_headers, "hist1", "Startpw1")

    from .helpers import login_with_captcha

    async def _as(token):
        return {"Authorization": f"Bearer {token}"}

    r = await login_with_captcha(client, "hist1", "Startpw1")
    await client.post("/api/v1/auth/change-password", json={
        "old_password": "Startpw1", "new_password": "Alpha1234",
    }, headers=await _as(r.json()["data"]["access_token"]))
    # 初始 Startpw1 + Alpha1234 在历史（count=2 时 trim 只留最近 2 个）

    r = await login_with_captcha(client, "hist1", "Alpha1234")
    await client.post("/api/v1/auth/change-password", json={
        "old_password": "Alpha1234", "new_password": "Beta12345",
    }, headers=await _as(r.json()["data"]["access_token"]))

    # 历史 = [Alpha1234, Beta12345]，改回 Alpha1234 → 拒绝
    r = await login_with_captcha(client, "hist1", "Beta12345")
    r = await client.post("/api/v1/auth/change-password", json={
        "old_password": "Beta12345", "new_password": "Alpha1234",
    }, headers=await _as(r.json()["data"]["access_token"]))
    assert r.status_code == 400 and "不能与最近" in r.json()["message"], r.text


# ---------------------------------------------------------------
# 管理员重置：角色权限门槛 + 免旧密
# ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_admin_reset_password_permission(client, admin_headers):
    roles = (await client.get("/api/v1/sys/roles", headers=admin_headers)).json()["data"]
    readonly = next(r for r in roles if r["code"] == "readonly")
    uid = await _create_user(client, admin_headers, "resetee", "Reset1234", role_ids=[readonly["id"]])

    from .helpers import login_with_captcha

    # 只读用户调用重置接口 → 403（无 sys:user:update）
    r = await login_with_captcha(client, "resetee", "Reset1234")
    vh = {"Authorization": f"Bearer {r.json()['data']['access_token']}"}
    r = await client.post(f"/api/v1/sys/users/{uid}/password", json={
        "new_password": "Hacked123",
    }, headers=vh)
    assert r.status_code == 403, r.text

    # 管理员重置（不能与初始历史密码相同）→ 成功，新密码可登录
    r = await client.post(f"/api/v1/sys/users/{uid}/password", json={
        "new_password": "Reset9976",
    }, headers=admin_headers)
    assert r.status_code == 200, r.text
    r = await login_with_captcha(client, "resetee", "Reset9976")
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------
# 定期强制改密：到期 → 登录标记 → 业务 428 → 改密豁免 → 恢复
# ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_password_expiry_blocks_business_api(client, admin_headers, monkeypatch):
    monkeypatch.setattr(settings, "password_expire_days", 90)
    monkeypatch.setattr(settings, "password_expire_warning_days", 7)

    roles = (await client.get("/api/v1/sys/roles", headers=admin_headers)).json()["data"]
    readonly = next(r for r in roles if r["code"] == "readonly")
    await _create_user(client, admin_headers, "expuser", "ExpStart1", role_ids=[readonly["id"]])

    from .helpers import login_with_captcha
    from repositories import users

    # 未到期：登录标记 false，业务正常
    r = await login_with_captcha(client, "expuser", "ExpStart1")
    assert r.json()["data"]["password_expired"] is False
    h = {"Authorization": f"Bearer {r.json()['data']['access_token']}"}
    r = await client.get("/api/v1/devices", headers=h)
    assert r.status_code == 200, r.text

    # 拨回 91 天 → 过期
    u = await users.get_by_username("expuser")
    await users.update(u["id"], {"password_changed_at": datetime.now(timezone.utc) - timedelta(days=91)})

    r = await login_with_captcha(client, "expuser", "ExpStart1")
    assert r.json()["data"]["password_expired"] is True, r.text
    h = {"Authorization": f"Bearer {r.json()['data']['access_token']}"}

    # 业务接口 → 428
    r = await client.get("/api/v1/devices", headers=h)
    assert r.status_code == 428, r.text

    # 自助改密豁免 → 200
    r = await client.post("/api/v1/auth/change-password", json={
        "old_password": "ExpStart1", "new_password": "Fresh1234",
    }, headers=h)
    assert r.status_code == 200, r.text

    # 改密后业务恢复
    r = await login_with_captcha(client, "expuser", "Fresh1234")
    h = {"Authorization": f"Bearer {r.json()['data']['access_token']}"}
    r = await client.get("/api/v1/devices", headers=h)
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------
# 临期警告 + 剩余天数（到期前 N 天登录标记）
# ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_password_expiry_warning(client, admin_headers, monkeypatch):
    monkeypatch.setattr(settings, "password_expire_days", 90)
    monkeypatch.setattr(settings, "password_expire_warning_days", 7)

    await _create_user(client, admin_headers, "warn1", "WarnStart1")
    from .helpers import login_with_captcha
    from repositories import users

    u = await users.get_by_username("warn1")
    await users.update(u["id"], {"password_changed_at": datetime.now(timezone.utc) - timedelta(days=86)})

    r = await login_with_captcha(client, "warn1", "WarnStart1")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["password_expired"] is False
    assert data["password_expire_in_days"] == 4
    assert data["password_expire_warning"] is True


# ---------------------------------------------------------------
# 定期改密在 session 模式同样生效（密码过期时间与认证方式无关）
# ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_password_expiry_works_in_session_mode(client, admin_headers, monkeypatch):
    # 先在 JWT 模式（当前 auth_mode）建用户，再切 session 模式，避免令牌模式不匹配
    await _create_user(client, admin_headers, "sespw", "SesStart1")
    monkeypatch.setattr(settings, "auth_mode", "session")
    monkeypatch.setattr(settings, "password_expire_days", 30)
    monkeypatch.setattr(settings, "password_expire_warning_days", 7)

    from .helpers import login_with_captcha
    from repositories import users

    r = await login_with_captcha(client, "sespw", "SesStart1")
    assert r.status_code == 200, r.text
    assert r.json()["data"].get("session_id")
    assert r.json()["data"]["password_expired"] is False

    u = await users.get_by_username("sespw")
    await users.update(u["id"], {"password_changed_at": datetime.now(timezone.utc) - timedelta(days=31)})
    r = await login_with_captcha(client, "sespw", "SesStart1")
    assert r.status_code == 200
    assert r.json()["data"]["password_expired"] is True
