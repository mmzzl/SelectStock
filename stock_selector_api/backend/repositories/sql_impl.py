"""SQL 通用 Repository 实现：基于 SQLAlchemy 异步 session。

子类声明 model + schema 即可获得完整 CRUD。自定义查询请只依赖
本类的通用助手（_find_one/_find_all/_list_from），保证业务层可跨 DB。
"""

from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import func, select, or_

from db.session import sessionmaker
from db.sql.base import Base


def _model_to_dict(obj: Base) -> dict:
    """ORM 实例 → dict（只含列，不含关联）。"""
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


class SqlRepository:
    model: type[Base] = None
    schema: Any = None  # 校验/字段白名单 schema（可选）

    def _filters_clause(self, filters: dict | None):
        if not filters:
            return None
        return [getattr(self.model, k) == v for k, v in filters.items() if v not in (None, "")]

    def _assign(self, obj: Base, data: dict) -> None:
        """只赋值 schema 声明过的字段（防 mass-assignment）。"""
        keys = self.schema.model_fields.keys() if self.schema else data.keys()
        for k in keys:
            if k in data and hasattr(obj, k):
                setattr(obj, k, data[k])

    @asynccontextmanager
    async def _session(self):
        sm = sessionmaker()
        async with sm() as s:
            yield s

    # ---------------- 通用查询助手（供实体自定义查询使用，跨 DB 语义一致） ----------------

    async def _find_one(self, filters: dict) -> dict | None:
        async with self._session() as s:
            conds = self._filters_clause(filters)
            stmt = select(self.model)
            if conds:
                stmt = stmt.where(*conds)
            obj = (await s.execute(stmt)).scalars().first()
            return _model_to_dict(obj) if obj else None

    async def _find_all(self, filters: dict | None = None, order_by: str | None = None) -> list[dict]:
        async with self._session() as s:
            conds = self._filters_clause(filters)
            stmt = select(self.model)
            if conds:
                stmt = stmt.where(*conds)
            if order_by and hasattr(self.model, order_by):
                stmt = stmt.order_by(getattr(self.model, order_by).asc())
            rows = (await s.execute(stmt)).scalars().all()
            return [_model_to_dict(o) for o in rows]

    async def _find_in(self, field: str, values: list[str]) -> list[dict]:
        if not values:
            return []
        async with self._session() as s:
            stmt = select(self.model).where(getattr(self.model, field).in_(values))
            rows = (await s.execute(stmt)).scalars().all()
            return [_model_to_dict(o) for o in rows]

    async def _list_from_in(self, table_name: str, field: str, values: list[str]) -> list[dict]:
        if not values:
            return []
        table = Base.metadata.tables.get(table_name)
        if table is None:
            return []
        async with self._session() as s:
            stmt = select(table).where(table.c[field].in_(values))
            rows = (await s.execute(stmt)).all()
            return [dict(r._mapping) for r in rows]

    async def _list_from(self, table_name: str, filters: dict) -> list[dict]:
        """从关联表读取原始行（如 user_roles/role_permissions）。"""
        table = Base.metadata.tables.get(table_name)
        if table is None:
            return []
        async with self._session() as s:
            stmt = select(table)
            for k, v in filters.items():
                if v not in (None, ""):
                    stmt = stmt.where(table.c[k] == v)
            rows = (await s.execute(stmt)).all()
            return [dict(r._mapping) for r in rows]

    async def _delete_from(self, table_name: str, filters: dict) -> None:
        table = Base.metadata.tables.get(table_name)
        if table is None:
            return
        from sqlalchemy import delete

        async with self._session() as s:
            stmt = delete(table)
            for k, v in filters.items():
                if v not in (None, ""):
                    stmt = stmt.where(table.c[k] == v)
            await s.execute(stmt)
            await s.commit()

    async def _update_where_one(self, table_name: str, where: dict, data: dict) -> bool:
        """原子条件更新：仅当 where 全部满足时更新，返回是否命中（乐观锁/用后即焚用）。"""
        table = Base.metadata.tables.get(table_name)
        if table is None:
            return False
        from sqlalchemy import update as sa_update

        async with self._session() as s:
            stmt = sa_update(table).where(
                *(table.c[k] == v for k, v in where.items() if v not in (None, ""))
            ).values(**data)
            res = await s.execute(stmt)
            await s.commit()
            return (res.rowcount or 0) > 0

    async def _insert_from(self, table_name: str, rows: list[dict]) -> None:
        table = Base.metadata.tables.get(table_name)
        if table is None or not rows:
            return
        async with self._session() as s:
            await s.execute(table.insert(), rows)
            await s.commit()

    async def _upsert_increment(self, table_name: str, filters: dict, inc_field: str) -> None:
        """数据库层原子「不存在则建(计数=1) + 存在则计数+1」。
        用 INSERT ... ON CONFLICT DO UPDATE，规避「先读后写」并发下双双 INSERT 触发唯一约束冲突。
        """
        import uuid

        from sqlalchemy.dialects.sqlite import insert as sa_insert

        table = Base.metadata.tables.get(table_name)
        if table is None:
            return
        where = {k: v for k, v in filters.items() if v not in (None, "")}
        stmt = sa_insert(table).values(id=uuid.uuid4().hex, **where, **{inc_field: 1})
        stmt = stmt.on_conflict_do_update(
            index_elements=[table.c.lock_key],
            set_={inc_field: table.c[inc_field] + 1},
        )
        async with self._session() as s:
            await s.execute(stmt)
            await s.commit()

    # ---------------- 标准 CRUD ----------------

    async def create(self, data: dict) -> dict:
        async with self._session() as s:
            obj = self.model()
            self._assign(obj, data)
            s.add(obj)
            await s.flush()   # 触发 DML（唯一约束冲突在 flush 时抛出，供上层捕获）
            await s.commit()
            # expire_on_commit=False，属性仍在；不 refresh，避免并发共享连接下 refresh 失败
            return _model_to_dict(obj)

    async def get(self, id: str) -> dict | None:
        async with self._session() as s:
            obj = await s.get(self.model, id)
            return _model_to_dict(obj) if obj else None

    async def list(
        self,
        *,
        filters: dict | None = None,
        keyword: str | None = None,
        keyword_fields: list[str] | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict], int]:
        page = max(1, page)
        size = min(max(1, size), 500)
        async with self._session() as s:
            conds = self._filters_clause(filters)
            stmt = select(self.model)
            count_stmt = select(func.count()).select_from(self.model)
            if conds:
                stmt = stmt.where(*conds)
                count_stmt = count_stmt.where(*conds)
            if keyword and keyword_fields:
                # 转义 LIKE 通配符，让用户输入按字面匹配（% 不再有通配能力）
                esc = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                like = or_(*(getattr(self.model, f).ilike(f"%{esc}%", escape="\\") for f in keyword_fields))
                stmt = stmt.where(like)
                count_stmt = count_stmt.where(like)
            total = (await s.execute(count_stmt)).scalar_one()
            order_col = getattr(self.model, "created_at", None) or self.model.id
            rows = (
                (
                    await s.execute(
                        stmt.order_by(order_col.desc()).offset((page - 1) * size).limit(size)
                    )
                )
                .scalars()
                .all()
            )
            return [_model_to_dict(o) for o in rows], total

    async def update(self, id: str, data: dict) -> dict | None:
        async with self._session() as s:
            obj = await s.get(self.model, id)
            if not obj:
                return None
            self._assign(obj, data)
            await s.commit()
            await s.refresh(obj)
            return _model_to_dict(obj)

    async def delete(self, id: str) -> bool:
        async with self._session() as s:
            obj = await s.get(self.model, id)
            if not obj:
                return False
            await s.delete(obj)
            await s.commit()
            return True

    async def count(self, filters: dict | None = None) -> int:
        async with self._session() as s:
            conds = self._filters_clause(filters)
            stmt = select(func.count()).select_from(self.model)
            if conds:
                stmt = stmt.where(*conds)
            return (await s.execute(stmt)).scalar_one()

    async def exists(self, filters: dict) -> bool:
        async with self._session() as s:
            conds = self._filters_clause(filters)
            stmt = select(func.count()).select_from(self.model)
            if conds:
                stmt = stmt.where(*conds)
            return (await s.execute(stmt)).scalar_one() > 0
