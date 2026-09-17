"""RBAC 管理服务：用户、角色、菜单、权限 的增删改查与分配。"""

from sqlalchemy.exc import IntegrityError

from core import security
from core.exceptions import BizError, NotFoundError, PermissionDeniedError
from repositories import users, roles, permissions, menus
from core.logger import log

# 内置角色（不可删除/改码，开箱即用）
BUILTIN_ROLES = {"superadmin", "admin", "readonly"}


# ---------------- 用户管理 ----------------

async def list_users(page: int, size: int, keyword: str | None):
    rows, total = await users.list(
        page=page, size=size, keyword=keyword, keyword_fields=["username", "display_name"]
    )
    items = []
    for u in rows:
        role_ids = await users.role_ids(u["id"])
        role_rows = await roles._find_in("id", role_ids)
        items.append({
            "id": u["id"],
            "username": u["username"],
            "display_name": u.get("display_name", ""),
            "email": u.get("email", ""),
            "is_active": u.get("is_active", True),
            "is_superuser": u.get("is_superuser", False),
            "role_ids": role_ids,
            "role_names": [r["name"] for r in role_rows],
            "created_at": u.get("created_at"),
            "updated_at": u.get("updated_at"),
        })
    return items, total


async def create_user(data: dict) -> dict:
    from services import password_policy

    if await users.get_by_username(data["username"]):
        raise BizError("用户名已存在")
    data = dict(data)
    password_policy.validate_password_strength(data["password"])
    data["password_hash"] = security.hash_password(data.pop("password"))
    data.pop("role_ids", None)
    try:
        user = await users.create(data)
    except IntegrityError:
        # 并发下唯一约束兜底（正常前置检查未命中时的竞态）
        raise BizError("用户名已存在")
    await password_policy.note_initial_password(user["id"], data["password_hash"])
    return user


async def update_user(user_id: str, data: dict, operator) -> dict:
    from services import password_policy

    data = dict(data)
    password = data.pop("password", None)
    role_ids = data.pop("role_ids", None)

    # 防止超管把自己降级/禁用导致系统锁死
    if user_id == operator.id and (data.get("is_superuser") is False or data.get("is_active") is False):
        raise BizError("不能禁用或降级当前登录的超管账号")

    if password:
        user = await users.get(user_id)
        if not user:
            raise NotFoundError("用户不存在")
        await password_policy.apply_password(user, password)

    user = await users.update(user_id, data)
    if not user:
        raise NotFoundError("用户不存在")
    if role_ids is not None:
        await users.set_roles(user_id, role_ids)
    return user


async def reset_password(user_id: str, new_password: str) -> None:
    """管理员重置他人密码（免旧密码，走统一策略+刷新改密时间）。"""
    from services import password_policy

    user = await users.get(user_id)
    if not user:
        raise NotFoundError("用户不存在")
    await password_policy.apply_password(user, new_password)


async def delete_user(user_id: str, operator) -> None:
    if user_id == operator.id:
        raise BizError("不能删除当前登录账号")
    await users.delete(user_id)


# ---------------- 角色管理 ----------------

async def list_roles() -> list[dict]:
    rows = await roles._find_all(order_by="created_at")
    out = []
    for r in rows:
        perm_ids = await roles.permission_ids(r["id"])
        perms = await permissions._find_in("id", perm_ids)
        out.append({
            "id": r["id"],
            "name": r["name"],
            "code": r["code"],
            "description": r.get("description", ""),
            "permission_codes": [p["code"] for p in perms],
            "builtin": r["code"] in BUILTIN_ROLES,
            "created_at": r.get("created_at"),
        })
    return out


async def create_role(data: dict) -> dict:
    if await roles.get_by_code(data["code"]):
        raise BizError("角色 code 已存在")
    return await roles.create(data)


async def update_role(role_id: str, data: dict) -> dict:
    r = await roles.get(role_id)
    if not r:
        raise NotFoundError("角色不存在")
    if r["code"] in BUILTIN_ROLES and "code" in data and data["code"] != r["code"]:
        raise BizError("内置角色 code 不可修改")
    return await roles.update(role_id, data)


async def delete_role(role_id: str) -> None:
    r = await roles.get(role_id)
    if not r:
        raise NotFoundError("角色不存在")
    if r["code"] in BUILTIN_ROLES:
        raise BizError("内置角色不可删除")
    await roles.delete(role_id)


async def assign_permissions(role_id: str, permission_ids: list[str]) -> None:
    r = await roles.get(role_id)
    if not r:
        raise NotFoundError("角色不存在")
    await roles.set_permissions(role_id, permission_ids)


# ---------------- 菜单管理 ----------------

async def list_menu_flat() -> list[dict]:
    return await menus.all_sorted()


async def menu_tree() -> list[dict]:
    return menus.build_tree(await menus.all_sorted())


async def create_menu(data: dict) -> dict:
    return await menus.create(data)


async def update_menu(menu_id: str, data: dict) -> dict:
    m = await menus.update(menu_id, data)
    if not m:
        raise NotFoundError("菜单不存在")
    return m


async def delete_menu(menu_id: str) -> None:
    if not await menus.delete(menu_id):
        raise NotFoundError("菜单不存在")


# ---------------- 权限管理 ----------------

async def list_permissions(module: str | None = None):
    if module:
        return await permissions.list_by_module(module)
    rows = await permissions._find_all(order_by="module")
    return rows


async def permission_modules() -> list[str]:
    return await permissions.module_list()


# 权限树展示名（参考产品 trees.xml / normal_tree.json 的树状分组；可按需扩展映射）
_PERM_MODULE_LABELS = {"sys": "系统管理", "device": "资产管理"}
_PERM_FEATURE_LABELS = {
    "user": "用户管理", "role": "角色管理", "menu": "菜单管理", "device": "设备管理",
}
_PERM_ACTION_LABELS = {"list": "查看", "create": "新增", "update": "修改", "delete": "删除"}


async def permission_tree() -> list[dict]:
    """权限树（模块 → 功能 → 动作），供角色分配页勾选。
    结构如 trees.xml/normal_tree.json 的树状权限配置，前端按此渲染复选框树。
    """
    modules: dict[str, dict] = {}
    rows = await permissions._find_all(order_by="code")
    for p in rows:
        parts = p["code"].split(":")
        module_key = parts[0]
        if len(parts) >= 3:
            # 三段式 sys:user:list → 功能=user、动作=list
            feature_key, action_key = parts[1], parts[2]
        else:
            # 两段式 device:list → 归到模块自身功能、第二段是动作
            feature_key = parts[0]
            action_key = parts[1] if len(parts) == 2 else "manage"
        m = modules.setdefault(module_key, {
            "module_key": module_key,
            "module": _PERM_MODULE_LABELS.get(module_key, module_key),
            "features": {},
        })
        f = m["features"].setdefault(feature_key, {
            "feature_key": feature_key,
            "feature": _PERM_FEATURE_LABELS.get(feature_key, feature_key),
            "permissions": [],
        })
        f["permissions"].append({
            "id": p["id"],
            "code": p["code"],
            "name": p["name"],
            "action_key": action_key,
            "action": _PERM_ACTION_LABELS.get(action_key, action_key),
        })
    return [
        {
            "module_key": mk,
            "module": m["module"],
            "features": [
                {
                    "feature_key": fk,
                    "feature": f["feature"],
                    "permissions": f["permissions"],
                }
                for fk, f in m["features"].items()
            ],
        }
        for mk, m in modules.items()
    ]


async def get_role(role_id: str) -> dict:
    """角色详情（含已分配的权限 id，供分配页回显勾选）。"""
    r = await roles.get(role_id)
    if not r:
        raise NotFoundError("角色不存在")
    perm_ids = await roles.permission_ids(role_id)
    perms = await permissions._find_in("id", perm_ids)
    return {
        "id": r["id"],
        "name": r["name"],
        "code": r["code"],
        "description": r.get("description", ""),
        "builtin": r["code"] in BUILTIN_ROLES,
        "permission_ids": perm_ids,
        "permission_codes": [p["code"] for p in perms],
        "created_at": r.get("created_at"),
    }
