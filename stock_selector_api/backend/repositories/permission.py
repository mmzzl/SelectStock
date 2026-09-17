"""权限 Repository：通用 CRUD + code 查询。"""

from db.sql.rbac import Permission
from repositories._choose import pick

RepoBase = pick()


class PermissionRepo(RepoBase):
    model = Permission
    table = "permissions"

    async def get_by_code(self, code: str) -> dict | None:
        return await self._find_one({"code": code})

    async def list_by_module(self, module: str) -> list[dict]:
        return await self._find_all({"module": module}, order_by="code")

    async def module_list(self) -> list[str]:
        rows = await self._find_all()
        return sorted({r.get("module") for r in rows if r.get("module")})
