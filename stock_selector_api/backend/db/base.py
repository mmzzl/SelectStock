"""数据库基座：按 DB_BACKEND 建立 SQLAlchemy 异步引擎(或 Motor client)。"""

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from config import settings
from core.logger import log

# ---------------------------------------------------------------
# SQLAlchemy
# ---------------------------------------------------------------

_engine: AsyncEngine | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings.database_url,
            echo=settings.debug,
            pool_pre_ping=True,
        )
        log.info("SQLAlchemy 引擎已创建: {}", settings.database_url)
    return _engine


async def dispose_engine() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


# ---------------------------------------------------------------
# MongoDB (Motor)
# ---------------------------------------------------------------

_motor_client = None


def get_mongo_client():
    global _motor_client
    if _motor_client is None:
        from motor.motor_asyncio import AsyncIOMotorClient

        _motor_client = AsyncIOMotorClient(settings.mongo_uri)
        log.info("Mongo client 已创建: {}", settings.mongo_uri)
    return _motor_client


def get_db() -> str:
    return settings.mongo_db


async def close_mongo_client() -> None:
    global _motor_client
    if _motor_client is not None:
        _motor_client.close()
        _motor_client = None
