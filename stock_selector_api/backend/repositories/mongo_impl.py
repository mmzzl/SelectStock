"""Mongo 通用 Repository 实现：基于 Motor。

子类声明 table(集合名) + schema 即可获得完整 CRUD。
自定义查询只依赖通用助手（_find_one/_find_all/_list_from），与 SQL 语义一致。
ID 统一 UUID hex 字符串（存 _id）。
"""

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from db.session import get_collection


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _doc_to_dict(doc: dict) -> dict:
    doc = dict(doc)
    doc["id"] = str(doc.pop("_id"))
    return doc


def _norm(filters: dict | None) -> dict:
    """把业务层的 id 过滤键映射到 mongo _id，保证与 SQL 语义一致。"""
    if not filters:
        return {}
    q = {}
    for k, v in filters.items():
        if v in (None, ""):
            continue
        q["_id" if k == "id" else k] = v
    return q


class MongoRepository:
    table: str = None
    schema: Any = None

    async def _coll(self):
        return get_collection(self.table)

    # ---------------- 通用查询助手 ----------------

    async def _find_one(self, filters: dict) -> dict | None:
        coll = await self._coll()
        doc = await coll.find_one(_norm(filters))
        return _doc_to_dict(doc) if doc else None

    async def _find_all(self, filters: dict | None = None, order_by: str | None = None) -> list[dict]:
        coll = await self._coll()
        cursor = coll.find(_norm(filters))
        if order_by:
            cursor = cursor.sort(order_by, 1)
        return [_doc_to_dict(d) async for d in cursor]

    async def _find_in(self, field: str, values: list[str]) -> list[dict]:
        if not values:
            return []
        coll = await self._coll()
        q = {"_id" if field == "id" else field: {"$in": values}}
        cursor = coll.find(q)
        return [_doc_to_dict(d) async for d in cursor]

    async def _list_from_in(self, table_name: str, field: str, values: list[str]) -> list[dict]:
        if not values:
            return []
        coll = get_collection(table_name)
        q = {"_id" if field == "id" else field: {"$in": values}}
        docs = [_doc_to_dict(d) async for d in coll.find(q)]
        return docs

    async def _list_from(self, table_name: str, filters: dict) -> list[dict]:
        """关联集合（user_roles/role_permissions）的原始行。"""
        coll = get_collection(table_name)
        docs = [_doc_to_dict(d) async for d in coll.find(_norm(filters))]
        return docs

    async def _delete_from(self, table_name: str, filters: dict) -> None:
        coll = get_collection(table_name)
        await coll.delete_many(_norm(filters))

    async def _update_where_one(self, table_name: str, where: dict, data: dict) -> bool:
        """原子条件更新（乐观锁/用后即焚）：仅当 where 满足时更新，返回是否命中。"""
        coll = get_collection(table_name)
        upd = {k: v for k, v in data.items() if v is not None}
        res = await coll.update_one(_norm(where), {"$set": upd})
        return (res.modified_count or 0) > 0

    async def _upsert_increment(self, table_name: str, filters: dict, inc_field: str) -> None:
        """数据库层原子「不存在则建(计数=1) + 存在则计数+1」。
        用 $inc + $setOnInsert + upsert，规避「先读后写」并发下重复建同 key 的冲突。
        """
        coll = get_collection(table_name)
        now = _now()
        q = _norm(filters)
        set_on_insert = dict(q)
        set_on_insert["_id"] = uuid.uuid4().hex
        set_on_insert["locked_until"] = None
        set_on_insert["created_at"] = now
        set_on_insert["updated_at"] = now
        await coll.update_one(
            q,
            {"$inc": {inc_field: 1}, "$setOnInsert": set_on_insert},
            upsert=True,
        )

    async def _insert_from(self, table_name: str, rows: list[dict]) -> None:
        if not rows:
            return
        coll = get_collection(table_name)
        docs = []
        for r in rows:
            d = dict(r)
            if "id" in d:
                d["_id"] = d.pop("id")
            d.setdefault("_id", uuid.uuid4().hex)
            docs.append(d)
        if len(docs) == 1:
            await coll.insert_one(docs[0])
        else:
            await coll.insert_many(docs)

    # ---------------- 标准 CRUD ----------------

    def _whitelist(self, data: dict) -> dict:
        """只保留 schema 声明过的字段 + 自动字段（与 SQL _assign 对齐，防 mass-assignment）。"""
        if not self.schema:
            return dict(data)
        keys = set(self.schema.model_fields.keys())
        kept = {k: v for k, v in data.items() if k in keys}
        for auto in ("id", "created_at", "updated_at"):
            if auto in data:
                kept[auto] = data[auto]
        return kept

    async def create(self, data: dict) -> dict:
        coll = await self._coll()
        now = _now()
        doc = self._whitelist(data)
        if "id" in doc:
            doc["_id"] = doc.pop("id")
        doc.setdefault("_id", uuid.uuid4().hex)
        doc.setdefault("created_at", now)
        doc.setdefault("updated_at", now)
        await coll.insert_one(doc)
        return _doc_to_dict(doc)

    async def get(self, id: str) -> dict | None:
        coll = await self._coll()
        doc = await coll.find_one({"_id": id})
        return _doc_to_dict(doc) if doc else None

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
        coll = await self._coll()
        q = _norm(filters)
        if keyword and keyword_fields:
            q["$or"] = [{f: {"$regex": re.escape(keyword), "$options": "i"}} for f in keyword_fields]
        total = await coll.count_documents(q)
        docs = [
            _doc_to_dict(d)
            async for d in coll.find(q).sort("created_at", -1).skip((page - 1) * size).limit(size)
        ]
        return docs, total

    async def update(self, id: str, data: dict) -> dict | None:
        coll = await self._coll()
        upd = self._whitelist(data)
        upd.pop("id", None)
        upd["updated_at"] = _now()
        res = await coll.find_one_and_update({"_id": id}, {"$set": upd}, return_document=True)
        return _doc_to_dict(res) if res else None

    async def delete(self, id: str) -> bool:
        coll = await self._coll()
        res = await coll.delete_one({"_id": id})
        return res.deleted_count > 0

    async def count(self, filters: dict | None = None) -> int:
        coll = await self._coll()
        return await coll.count_documents(_norm(filters))

    async def exists(self, filters: dict) -> bool:
        coll = await self._coll()
        return await coll.count_documents(_norm(filters)) > 0
