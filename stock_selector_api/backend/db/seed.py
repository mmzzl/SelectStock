"""数据初始化：角色、权限、菜单、超管、示例设备。幂等（重复启动不重复插入）。"""

from datetime import datetime, timezone

from core import security
from core.logger import log
from repositories import users, roles, permissions, menus, devices

# 角色：三档开箱即用
INIT_ROLES = [
    {"name": "系统管理员", "code": "superadmin", "description": "拥有全部权限，负责系统配置与用户管理"},
    {"name": "普通管理员", "code": "admin", "description": "负责业务功能日常运维"},
    {"name": "只读用户", "code": "readonly", "description": "只能查看，不能修改"},
]

# 权限：按模块分组
INIT_PERMISSIONS = [
    # 系统模块
    {"code": "sys:user:list", "name": "用户列表", "module": "system"},
    {"code": "sys:user:create", "name": "新增用户", "module": "system"},
    {"code": "sys:user:update", "name": "修改用户", "module": "system"},
    {"code": "sys:user:delete", "name": "删除用户", "module": "system"},
    {"code": "sys:role:list", "name": "角色列表", "module": "system"},
    {"code": "sys:role:create", "name": "新增角色", "module": "system"},
    {"code": "sys:role:update", "name": "修改角色", "module": "system"},
    {"code": "sys:role:delete", "name": "删除角色", "module": "system"},
    {"code": "sys:menu:list", "name": "菜单列表", "module": "system"},
    {"code": "sys:menu:create", "name": "新增菜单", "module": "system"},
    {"code": "sys:menu:update", "name": "修改菜单", "module": "system"},
    {"code": "sys:menu:delete", "name": "删除菜单", "module": "system"},
    # 通知模块
    {"code": "sys:notify:list", "name": "通知配置查看", "module": "system"},
    {"code": "sys:notify:update", "name": "通知配置管理", "module": "system"},
    # 设备模块（示例）
    {"code": "device:list", "name": "设备列表", "module": "device"},
    {"code": "device:create", "name": "新增设备", "module": "device"},
    {"code": "device:update", "name": "修改设备", "module": "device"},
    {"code": "device:delete", "name": "删除设备", "module": "device"},
]

# 菜单树：id 固定，便于权限关联与树形构建
INIT_MENUS = [
    # 一级目录
    {"id": "m_dashboard", "name": "工作台", "menu_type": "menu", "path": "#/dashboard", "icon": "home", "parent_id": "", "sort_order": 1, "permission_code": "", "is_visible": True},
    {"id": "m_device", "name": "资产管理", "menu_type": "dir", "path": "", "icon": "server", "parent_id": "", "sort_order": 2, "permission_code": "", "is_visible": True},
    {"id": "m_sys", "name": "系统管理", "menu_type": "dir", "path": "", "icon": "setting", "parent_id": "", "sort_order": 99, "permission_code": "", "is_visible": True},
    # 设备子菜单
    {"id": "m_device_list", "name": "设备列表", "menu_type": "menu", "path": "#/devices", "icon": "", "parent_id": "m_device", "sort_order": 1, "permission_code": "device:list", "is_visible": True},
    # 系统子菜单
    {"id": "m_sys_users", "name": "用户管理", "menu_type": "menu", "path": "#/users", "icon": "", "parent_id": "m_sys", "sort_order": 1, "permission_code": "sys:user:list", "is_visible": True},
    {"id": "m_sys_roles", "name": "角色管理", "menu_type": "menu", "path": "#/roles", "icon": "", "parent_id": "m_sys", "sort_order": 2, "permission_code": "sys:role:list", "is_visible": True},
    {"id": "m_sys_menus", "name": "菜单管理", "menu_type": "menu", "path": "#/menus", "icon": "", "parent_id": "m_sys", "sort_order": 3, "permission_code": "sys:menu:list", "is_visible": True},
    {"id": "m_sys_notify", "name": "通知配置", "menu_type": "menu", "path": "#/notify/channels", "icon": "", "parent_id": "m_sys", "sort_order": 4, "permission_code": "sys:notify:list", "is_visible": True},
    {"id": "m_sys_notify_policy", "name": "订阅策略", "menu_type": "menu", "path": "#/notify/policy", "icon": "", "parent_id": "m_sys", "sort_order": 5, "permission_code": "sys:notify:list", "is_visible": True},
]

# 角色拥有的权限（按 code 关联），超级管理员不用绑定（is_superuser 通配）
ROLE_PERMISSION_CODES = {
    "superadmin": [],  # 通配 *
    "admin": [
        "sys:user:list", "sys:role:list", "sys:menu:list",
        "sys:notify:list", "sys:notify:update",
        "device:list", "device:create", "device:update", "device:delete",
    ],
    "readonly": ["device:list"],
}

INIT_DEVICES = [
    {"name": "核心交换机-01", "ip": "10.0.0.1", "vendor": "Huawei", "model": "S12700", "location": "机房A", "status": "online", "owner": "网络组", "remark": "核心汇聚"},
    {"name": "防火墙-01", "ip": "10.0.0.2", "vendor": "Sangfor", "model": "AF-2000", "location": "机房A", "status": "online", "owner": "安全组", "remark": ""},
    {"name": "Web服务器", "ip": "10.0.1.10", "vendor": "DELL", "model": "R740", "location": "机房B", "status": "offline", "owner": "运维组", "remark": "计划停机维护"},
]


async def _create_if_missing(repo, matcher: dict, data: dict) -> str:
    """存在则返回 id，不存在则创建，返回 id。"""
    existing = await repo._find_one(matcher)
    if existing:
        return existing["id"]
    created = await repo.create({**data, **matcher})
    return created["id"]


async def seed_all() -> None:
    """幂等初始化：角色→权限→菜单→超管→绑定→示例数据。"""
    # 角色
    role_ids = {}
    for r in INIT_ROLES:
        rid = await _create_if_missing(roles, {"code": r["code"]}, r)
        role_ids[r["code"]] = rid

    # 权限
    perm_ids = {}
    for p in INIT_PERMISSIONS:
        pid = await _create_if_missing(permissions, {"code": p["code"]}, p)
        perm_ids[p["code"]] = pid

    # 给非超管角色绑定权限
    for code, pcodes in ROLE_PERMISSION_CODES.items():
        if not pcodes:
            continue
        rid = role_ids[code]
        target_ids = [perm_ids[c] for c in pcodes if c in perm_ids]
        await roles.set_permissions(rid, target_ids)

    # 菜单
    for m in INIT_MENUS:
        await _create_if_missing(menus, {"id": m["id"]}, {k2: v for k2, v in m.items() if k2 != "id"})

    # 超管账号
    super_user = await users.get_by_username("admin")
    if not super_user:
        await users.create({
            "username": "admin",
            "password_hash": security.hash_password("admin123"),
            "display_name": "系统管理员",
            "email": "",
            "is_active": True,
            "is_superuser": True,
            "password_changed_at": datetime.now(timezone.utc),
        })
        log.info("已创建默认超管账号 admin/admin123")

    # 示例设备（演示模块数据，幂等：按名字判断）
    for d in INIT_DEVICES:
        existing = await devices._find_one({"name": d["name"]})
        if not existing:
            await devices.create(d)

    log.info("数据初始化完成")
