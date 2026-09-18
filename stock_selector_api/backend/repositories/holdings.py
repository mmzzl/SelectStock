"""repositories/holdings.py — 持仓仓储（属主隔离；enabled 过滤供诊断取数）。"""
from __future__ import annotations

from db.sql.holdings import Holding
from repositories._choose import pick
from schemas.holdings import HoldingCreate

RepoBase = pick()


class HoldingRepo(RepoBase):
    model = Holding
    table = "holdings"
    schema = HoldingCreate

    async def list_by_user(self, user_id: str) -> list[dict]:
        return await self._find_all({"user_id": user_id}, order_by="created_at")

    async def list_enabled(self, user_id: str) -> list[dict]:
        return await self._find_all({"user_id": user_id, "enabled": True}, order_by="created_at")

    async def find_by_user_code(self, user_id: str, code: str) -> dict | None:
        return await self._find_one({"user_id": user_id, "code": code})

    async def count_by_user(self, user_id: str) -> int:
        return await self.count({"user_id": user_id})


holdings_repo = HoldingRepo()