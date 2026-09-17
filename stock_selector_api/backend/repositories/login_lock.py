"""账户锁定 Repository：按维度键查询/递增/解锁（原子条件更新）。"""

from db.sql.login_lock import LoginLock
from repositories._choose import pick

RepoBase = pick()


class LoginLockRepo(RepoBase):
    model = LoginLock
    table = "login_locks"

    async def get_by_key(self, lock_key: str) -> dict | None:
        return await self._find_one({"lock_key": lock_key})

    async def increment_if_not_locked(self, lock_key: str) -> None:
        """原子递增失败计数：不存在则创建为 1，存在则 +1。
        走 DB 层 upsert-increment，避免并发下「先读后写」双双 INSERT 触发唯一约束冲突（HTTP 500 / 丢计数）。
        """
        await self._upsert_increment("login_locks", {"lock_key": lock_key}, "fail_count")

    async def set_locked(self, lock_key: str, locked_until, fail_count: int) -> None:
        await self._update_where_one(
            "login_locks", {"lock_key": lock_key},
            {"fail_count": fail_count, "locked_until": locked_until},
        )

    async def clear_key(self, lock_key: str) -> None:
        await self._delete_from("login_locks", {"lock_key": lock_key})
