"""RBAC 聚合：用户权限码集合 + 可见菜单树。

全部使用实体 Repo 的通用助手拼装，业务层不感知 sqlite/mongodb。
"""

from repositories.user import UserRepo
from repositories.role import RoleRepo
from repositories.permission import PermissionRepo
from repositories.menu import MenuRepo


class RbacRepo:
    def __init__(self):
        self.users = UserRepo()
        self.roles = RoleRepo()
        self.permissions = PermissionRepo()
        self.menus = MenuRepo()

    async def permission_codes_of_user(self, user_id: str) -> set[str]:
        """用户拥有的全部权限码（直接权限角色聚合）。超管返回通配。"""
        user = await self.users.get(user_id)
        if not user:
            return set()
        if user.get("is_superuser"):
            return {"*"}
        role_ids = await self.users.role_ids(user_id)
        if not role_ids:
            return set()
        perm_ids = []
        for rid in role_ids:
            perm_ids += await self.roles.permission_ids(rid)
        if not perm_ids:
            return set()
        perms = await self.permissions._find_in("id", list(set(perm_ids)))
        return {p["code"] for p in perms}

    async def visible_menus_of_user(self, user_id: str) -> list[dict]:
        """当前用户可看到的菜单树（含按钮，超管看全部）。"""
        all_menus = await self.menus.all_sorted()
        user = await self.users.get(user_id)
        visible = set(await self.permission_codes_of_user(user_id)) if user else set()

        def _keep(menu: dict) -> bool:
            if not menu.get("is_visible", True):
                return False
            if "*" in visible:
                return True
            code = menu.get("permission_code") or ""
            # 无权限码的纯导航节点（如目录）始终可见
            if not code:
                return True
            # 菜单可配置多个权限码（逗号分隔，任一满足即可见）
            return any(p in visible for p in code.split(",") if p)

        kept = [m for m in all_menus if _keep(m)]
        tree = self.menus.build_tree(kept)
        return _prune_empty_dirs(tree)

    async def permission_codes_of_user_flat(self, user_id: str) -> list[str]:
        return sorted(await self.permission_codes_of_user(user_id))


def _prune_empty_dirs(nodes: list[dict]) -> list[dict]:
    """递归移除没有可见子项的目录节点（避免给用户显示空分组）。"""
    out = []
    for m in nodes:
        children = m.get("children") or []
        m["children"] = _prune_empty_dirs(children)
        if m.get("menu_type") == "dir" and not m["children"]:
            continue  # 空目录不显示
        out.append(m)
    return out
