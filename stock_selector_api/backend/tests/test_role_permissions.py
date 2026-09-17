"""角色↔权限分配测试：权限树形状、角色详情回显、分配后生效。"""

import pytest


@pytest.mark.asyncio
async def test_permission_tree_shape(client, admin_headers):
    r = await client.get("/api/v1/sys/permissions/tree", headers=admin_headers)
    assert r.status_code == 200, r.text
    tree = r.json()["data"]
    assert isinstance(tree, list) and tree
    module = tree[0]
    assert {"module", "module_key", "features"} <= module.keys()
    assert module["features"] and {"feature", "feature_key", "permissions"} <= module["features"][0].keys()

    codes = [p["code"] for m in tree for f in m["features"] for p in f["permissions"]]
    assert "sys:user:list" in codes
    assert "device:list" in codes


@pytest.mark.asyncio
async def test_permission_tree_grouped_by_module(client, admin_headers):
    """不同模块的权限归到不同 module 节点（好分配：按模块/功能分组勾选）。"""
    r = await client.get("/api/v1/sys/permissions/tree", headers=admin_headers)
    tree = r.json()["data"]
    by_module = {m["module_key"]: m for m in tree}
    assert "sys" in by_module and "device" in by_module
    sys_codes = [p["code"] for f in by_module["sys"]["features"] for p in f["permissions"]]
    dev_codes = [p["code"] for f in by_module["device"]["features"] for p in f["permissions"]]
    assert all(c.startswith("sys:") for c in sys_codes)
    assert all(c.startswith("device:") for c in dev_codes)
    # 两段式 device:* 归到同一功能（设备管理）下
    sys_feature_keys = [f["feature_key"] for f in by_module["sys"]["features"]]
    assert "user" in sys_feature_keys and "role" in sys_feature_keys and "menu" in sys_feature_keys


@pytest.mark.asyncio
async def test_role_detail_returns_permission_ids(client, admin_headers):
    roles = (await client.get("/api/v1/sys/roles", headers=admin_headers)).json()["data"]
    admin = next(x for x in roles if x["code"] == "admin")
    detail = (await client.get(f"/api/v1/sys/roles/{admin['id']}", headers=admin_headers)).json()["data"]
    assert detail["permission_ids"]
    assert detail["permission_codes"]


@pytest.mark.asyncio
async def test_assign_permissions_changes_role_and_menus(client, admin_headers):
    # 建一个空权限角色
    r = await client.post("/api/v1/sys/roles", json={
        "name": "临时角色", "code": "temp_role", "description": "",
    }, headers=admin_headers)
    assert r.status_code == 200, r.text
    rid = r.json()["data"]["id"]

    # 初始无权限、无菜单
    detail = (await client.get(f"/api/v1/sys/roles/{rid}", headers=admin_headers)).json()["data"]
    assert detail["permission_ids"] == []
    assert detail["permission_codes"] == []

    # 分配 device:list
    perms = (await client.get("/api/v1/sys/permissions", headers=admin_headers)).json()["data"]
    dev = next(p for p in perms if p["code"] == "device:list")
    r = await client.post(f"/api/v1/sys/roles/{rid}/permissions", json={"permission_ids": [dev["id"]]},
                          headers=admin_headers)
    assert r.status_code == 200, r.text

    # 详情回显已含
    detail = (await client.get(f"/api/v1/sys/roles/{rid}", headers=admin_headers)).json()["data"]
    assert detail["permission_ids"] == [dev["id"]]
    assert detail["permission_codes"] == ["device:list"]

    # 分配后联动：该角色下用户的动态菜单出现对应菜单（角色→可见页面）
    uid = (await client.post("/api/v1/sys/users", json={
        "username": "tempviewer", "password": "Viewer123", "role_ids": [rid],
    }, headers=admin_headers)).json()["data"]["id"]
    from repositories import rbac

    def _collect(nodes):
        out = []
        for n in nodes:
            out.append(n.get("code") or n.get("permission_code") or "")
            out += _collect(n.get("children") or [])
        return out

    menu_codes = _collect(await rbac.visible_menus_of_user(uid))
    assert any("device" in c for c in menu_codes), menu_codes
