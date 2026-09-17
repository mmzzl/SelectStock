"""菜单 Repository：通用 CRUD + 树形查询。"""

from db.sql.rbac import Menu
from repositories._choose import pick

RepoBase = pick()


class MenuRepo(RepoBase):
    model = Menu
    table = "menus"

    async def all_sorted(self) -> list[dict]:
        rows = await self._find_all(order_by="sort_order")
        return sorted(rows, key=lambda m: m.get("sort_order", 0))

    async def children_of(self, parent_id: str) -> list[dict]:
        return await self._find_all({"parent_id": parent_id}, order_by="sort_order")

    def build_tree(self, menus: list[dict]) -> list[dict]:
        """内存建树（DB 无关的纯 Python）。返回带 children 的树。"""
        by_id = {m["id"]: {**m, "children": []} for m in menus}
        roots: list[dict] = []
        for m in by_id.values():
            pid = m.get("parent_id")
            if pid and pid in by_id:
                by_id[pid]["children"].append(m)
            else:
                roots.append(m)
        return roots
