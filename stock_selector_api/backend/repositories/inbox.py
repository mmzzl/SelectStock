"""repositories/inbox.py — 站内信仓储（属主隔离；列表/未读计数/已读/since_id 增量）。"""
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy import update as sa_update

from db.sql.inbox import InboxMessage
from repositories._choose import pick
from repositories.sql_impl import _model_to_dict
from schemas.inbox import InboxCreate

RepoBase = pick()


def _read_at() -> datetime:
    return datetime.now(timezone.utc)


class InboxRepo(RepoBase):
    model = InboxMessage
    table = "inbox_messages"
    schema = InboxCreate

    async def unread_count(self, user_id: str) -> int:
        async with self._session() as s:
            stmt = (select(func.count()).select_from(self.model)
                    .where(self.model.user_id == user_id, self.model.read_at.is_(None)))
            return (await s.execute(stmt)).scalar_one()

    async def latest_id(self, user_id: str) -> str | None:
        async with self._session() as s:
            stmt = (select(self.model.id).where(self.model.user_id == user_id)
                    .order_by(self.model.created_at.desc()).limit(1))
            return (await s.execute(stmt)).scalar_one_or_none()

    async def mark_read(self, msg_id: str, user_id: str) -> bool:
        return await self._update_where_one(
            self.table, {"id": msg_id, "user_id": user_id}, {"read_at": _read_at()})

    async def mark_all_read(self, user_id: str) -> None:
        async with self._session() as s:
            await s.execute(sa_update(self.model).where(
                self.model.user_id == user_id, self.model.read_at.is_(None)
            ).values(read_at=_read_at()))
            await s.commit()

    async def list_for(self, user_id: str, *, page: int = 1, size: int = 20,
                       unread_only: bool = False, since_id: str | None = None
                       ) -> tuple[list[dict], int, int]:
        page = max(1, page)
        size = min(max(1, size), 500)
        async with self._session() as s:
            base = select(self.model).where(self.model.user_id == user_id)
            count_stmt = select(func.count()).select_from(self.model).where(self.model.user_id == user_id)
            unread_stmt = (select(func.count()).select_from(self.model)
                           .where(self.model.user_id == user_id, self.model.read_at.is_(None)))
            if unread_only:
                base = base.where(self.model.read_at.is_(None))
                count_stmt = count_stmt.where(self.model.read_at.is_(None))
            if since_id:
                ref = await s.get(self.model, since_id)
                if ref is not None:
                    base = base.where(self.model.created_at > ref.created_at)
                    count_stmt = count_stmt.where(self.model.created_at > ref.created_at)
            total = (await s.execute(count_stmt)).scalar_one()
            unread = (await s.execute(unread_stmt)).scalar_one()
            rows = (await s.execute(
                base.order_by(self.model.created_at.desc())
                    .offset((page - 1) * size).limit(size))).scalars().all()
            return [_model_to_dict(o) for o in rows], total, unread


inbox_repo = InboxRepo()
