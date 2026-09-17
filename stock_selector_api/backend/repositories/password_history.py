"""密码历史 Repository：记录/查询每人最近 N 个旧密码哈希（拒绝重用）。"""

from datetime import datetime, timezone

from db.sql.password_history import PasswordHistory
from repositories._choose import pick

RepoBase = pick()


class PasswordHistoryRepo(RepoBase):
    model = PasswordHistory
    table = "password_history"

    async def add(self, user_id: str, password_hash: str) -> None:
        await self.create({
            "user_id": user_id,
            "password_hash": password_hash,
            "created_at": datetime.now(timezone.utc),
        })

    async def recent_hashes(self, user_id: str, limit: int) -> list[str]:
        """按时间倒序返回最近 limit 个旧密码哈希。"""
        rows = await self._find_all({"user_id": user_id}, order_by="created_at")
        latest = [r["password_hash"] for r in rows][-limit:][::-1]
        return latest

    async def trim(self, user_id: str, keep: int) -> None:
        """只保留最近 keep 条历史，更早的删除（防表膨胀）。"""
        rows = await self._find_all({"user_id": user_id}, order_by="created_at")
        for r in rows[:-keep]:
            await self._delete_from("password_history", {"id": r["id"]})
