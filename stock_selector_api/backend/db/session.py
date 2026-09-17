"""DB 会话访问器：SQL 用 async_sessionmaker，Mongo 返回 collection 工厂。

统一对外：
  - session()        → 返回 SessionMaker（SQL）
  - get_collection(name) → 返回 Motor collection；在调用方内 async with await client.start_session()
只允许 db/ 与 repositories/ 使用本模块，其他层禁止 import。
"""

from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from config import settings
from db.base import get_engine, get_mongo_client, get_db

_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(), expire_on_commit=False
        )
    return _sessionmaker


def sessionmaker() -> async_sessionmaker[AsyncSession]:
    return get_sessionmaker()


def get_collection(name: str):
    """Mongo collection 访问器（仅 mongodb 后端使用）。"""
    return get_mongo_client()[get_db()][name]


def is_mongodb() -> bool:
    return settings.is_mongodb()
