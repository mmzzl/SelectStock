"""冒烟测试：登录 → 动态菜单 → 设备 CRUD → 权限隔离 → 刷新 → session 模式 + 验证码。"""

import base64

import pytest

from .helpers import login_with_captcha


# ---------------------------------------------------------------
# 认证
# ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_login_and_me(client):
    r = await login_with_captcha(client, "admin", "admin123")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["access_token"]

    headers = {"Authorization": f"Bearer {data['access_token']}"}
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200
    body = me.json()["data"]
    assert body["username"] == "admin"
    assert body["is_superuser"] is True


@pytest.mark.asyncio
async def test_login_wrong_password(client):
    r = await login_with_captcha(client, "admin", "wrong")
    assert r.status_code == 401
    assert r.json()["success"] is False


@pytest.mark.asyncio
async def test_unauthenticated_forbidden(client):
    r = await client.get("/api/v1/devices")
    assert r.status_code == 401  # 未带 token → AuthError 401


@pytest.mark.asyncio
async def test_refresh_token(client):
    r = await login_with_captcha(client, "admin", "admin123")
    refresh = r.json()["data"]["refresh_token"]
    r2 = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert r2.status_code == 200
    assert r2.json()["data"]["access_token"]


# ---------------------------------------------------------------
# 动态菜单
# ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_dynamic_menus_superadmin(client, admin_headers):
    r = await client.get("/api/v1/auth/menus", headers=admin_headers)
    assert r.status_code == 200
    tree = r.json()["data"]
    # 超管能看到全部（至少含工作台 + 资产管理目录 + 系统管理目录）
    names = {m["name"] for m in tree}
    assert "工作台" in names
    assert "资产管理" in names
    assert "系统管理" in names


# ---------------------------------------------------------------
# 设备 CRUD（示例模块）
# ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_device_crud(client, admin_headers):
    # create
    r = await client.post("/api/v1/devices", json={"name": "测试设备", "ip": "192.168.1.100"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    dev_id = r.json()["data"]["id"]

    # list
    r = await client.get("/api/v1/devices?keyword=测试设备", headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["data"]["total"] >= 1

    # update
    r = await client.put(f"/api/v1/devices/{dev_id}", json={"owner": "安全组"}, headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["data"]["owner"] == "安全组"

    # delete
    r = await client.delete(f"/api/v1/devices/{dev_id}", headers=admin_headers)
    assert r.status_code == 200


# ---------------------------------------------------------------
# 权限隔离（三档角色开箱即用）
# ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_user_roles_permission_isolation(client, admin_headers):
    # 管理员建一个只读用户并绑定 readonly 角色
    roles = (await client.get("/api/v1/sys/roles", headers=admin_headers)).json()["data"]
    readonly = next(r for r in roles if r["code"] == "readonly")

    r = await client.post("/api/v1/sys/users", json={
        "username": "viewer1", "password": "Viewer123",
        "display_name": "只读用户", "role_ids": [readonly["id"]],
    }, headers=admin_headers)
    assert r.status_code == 200, r.text

    # 只读用户登录（走验证码链路）
    r = await login_with_captcha(client, "viewer1", "Viewer123")
    assert r.status_code == 200, r.text
    token = r.json()["data"]["access_token"]
    vh = {"Authorization": f"Bearer {token}"}

    # 可以看设备列表（readonly 有 device:list）
    r = await client.get("/api/v1/devices", headers=vh)
    assert r.status_code == 200

    # 不能创建设备（readonly 无 device:create）→ 403
    r = await client.post("/api/v1/devices", json={"name": "越权设备"}, headers=vh)
    assert r.status_code == 403
    assert r.json()["message"]

    # 只读用户动态菜单中不应该出现"菜单管理"（无 sys:menu:list）
    r = await client.get("/api/v1/auth/menus", headers=vh)
    assert r.status_code == 200
    flat = deflate(r.json()["data"])
    assert "菜单管理" not in flat
    # 无权限的子菜单被过滤后，空目录也要被 prune 掉（不应出现"系统管理"空分组）
    assert "系统管理" not in flat


@pytest.mark.asyncio
async def test_concurrent_same_username_graceful(client, admin_headers):
    """并发创建同名用户：唯一约束冲突应优雅返回 400，而非 500。"""
    import asyncio

    async def _create():
        return await client.post("/api/v1/sys/users", json={
            "username": "race_me", "password": "Race12345", "display_name": "并发",
        }, headers=admin_headers)

    results = await asyncio.gather(*[_create() for _ in range(5)])
    codes = [r.status_code for r in results]
    assert all(code in (200, 400) for code in codes), codes
    assert 200 in codes  # 至少一个成功
    assert 400 in codes  # 其余应干净返回已存在，而非 500


# ---------------------------------------------------------------
# session 认证模式
# ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_auth_mode(client):
    from config import settings

    orig = settings.auth_mode
    settings.auth_mode = "session"
    try:
        r = await login_with_captcha(client, "admin", "admin123")
        assert r.status_code == 200, r.text
        # session 模式应该有 cookie && 响应体 session_id
        data = r.json()["data"]
        assert data.get("session_id")
        assert settings.session_cookie_name in client.cookies

        # 不带 cookie 访问 → 未登录（清掉 client 里共享的 cookie）
        client.cookies.clear()
        r2_headers = {"Cookie": "nosuch=1"}
        r2 = await client.get("/api/v1/devices", headers=r2_headers)
        assert r2.status_code == 401
    finally:
        settings.auth_mode = orig
        client.cookies.clear()


# ---------------------------------------------------------------
# 验证码（防伪造：会话绑定 + 用后即焚 + 大小写不敏感）
# ---------------------------------------------------------------

async def _issue(client):
    """取一张验证码，返回 (captcha_id, 明文code)。"""
    r = await client.get("/api/v1/auth/captcha")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["captcha_id"]
    assert data["image_base64"]

    from repositories import captchas

    rec = await captchas.get_by_id(data["captcha_id"])
    return data["captcha_id"], rec["code"]


@pytest.mark.asyncio
async def test_captcha_issue_returns_png(client):
    r = await client.get("/api/v1/auth/captcha")
    assert r.status_code == 200
    data = r.json()["data"]
    raw = base64.b64decode(data["image_base64"])
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"  # PNG 魔数


@pytest.mark.asyncio
async def test_captcha_case_insensitive(client):
    cid, code = await _issue(client)
    body = {"username": "admin", "password": "admin123",
            "captcha_id": cid, "captcha_code": code.swapcase()}
    r = await client.post("/api/v1/auth/login", json=body)
    assert r.status_code == 200, r.text  # 大小写不敏感仍能登


@pytest.mark.asyncio
async def test_captcha_wrong_code_rejected(client):
    cid, _ = await _issue(client)
    body = {"username": "admin", "password": "admin123",
            "captcha_id": cid, "captcha_code": "XXXX"}
    r = await client.post("/api/v1/auth/login", json=body)
    assert r.status_code == 400
    assert "验证码" in r.json()["message"]


@pytest.mark.asyncio
async def test_captcha_missing_fields_rejected(client):
    body = {"username": "admin", "password": "admin123"}  # 没带验证码
    r = await client.post("/api/v1/auth/login", json=body)
    assert r.status_code == 400
    assert "验证码" in r.json()["message"]


@pytest.mark.asyncio
async def test_captcha_single_use(client):
    """用后即焚：同一 captcha_id 用第二次必须被拒。"""
    cid, code = await _issue(client)
    body = {"username": "admin", "password": "admin123",
            "captcha_id": cid, "captcha_code": code}
    ok = await client.post("/api/v1/auth/login", json=body)
    assert ok.status_code == 200
    again = await client.post("/api/v1/auth/login", json=body)
    assert again.status_code == 400
    assert "已使用" in again.json()["message"]


@pytest.mark.asyncio
async def test_captcha_session_binding(client):
    """防伪造核心：换一个匿名会话就校验失败（跨客户端无法复用）。"""
    cid, code = await _issue(client)
    fake_session = {"Cookie": "anon_sid=attacker-controlled-sid"}
    body = {"username": "admin", "password": "admin123",
            "captcha_id": cid, "captcha_code": code}
    r = await client.post("/api/v1/auth/login", json=body, headers=fake_session)
    assert r.status_code == 400
    assert "会话不匹配" in r.json()["message"]


def deflate(tree, acc=None):
    """把菜单树展开为名列表（用于断言可见性）。"""
    if acc is None:
        acc = []
    for m in tree:
        acc.append(m["name"])
        if m.get("children"):
            deflate(m["children"], acc)
    return acc
