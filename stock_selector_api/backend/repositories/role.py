"""角色 Repository：通用 CRUD + code 查询 + 权限关系维护。"""

from db.sql.rbac import Role
from repositories._choose import pick

RepoBase = pick()


class RoleRepo(RepoBase):
    model = Role
    table = "roles"

    async def get_by_code(self, code: str) -> dict | None:
        return await self._find_one({"code": code})

    async def permission_ids(self, role_id: str) -> list[str]:
        rows = await self._list_from("role_permissions", {"role_id": role_id})
        return [r["permission_id"] for r in rows]

    async def set_permissions(self, role_id: str, permission_ids: list[str]) -> None:
        await self._delete_from("role_permissions", {"role_id": role_id})
        if permission_ids:
            await self._insert_from(
                "role_permissions",
                [{"role_id": role_id, "permission_id": pid} for pid in permission_ids],
            )
