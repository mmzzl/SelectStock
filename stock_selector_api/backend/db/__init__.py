"""数据库统一入口：按 DB_BACKEND 路由到 sqlite(SQLAlchemy) 或 mongodb(Motor)。

对外只暴露四个函数，业务层不感知底层。
"""

from config import settings
from core.logger import log


async def create_all() -> None:
    """建表（仅 sqlite 需要；mongodb 无表结构，只建索引）。"""
    if settings.is_sqlite():
        from db.sql import Base
        from db.base import get_engine

        async with get_engine().begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(_ensure_user_password_changed_at)
        log.info("SQLite 建表完成")
    else:
        from db.mongo.indexes import ensure_indexes

        await ensure_indexes()


def _ensure_user_password_changed_at(sync_conn) -> None:
    """老库补列：create_all 只建新表不会给既有表加列，这里幂等地补 ALTER。
    存量用户用建号时间回填改密时间（否则 NULL→视为新密码永不过期）。
    """
    from sqlalchemy import inspect, text

    cols = {c["name"] for c in inspect(sync_conn).get_columns("users")}
    if "password_changed_at" not in cols:
        sync_conn.execute(text("ALTER TABLE users ADD COLUMN password_changed_at DATETIME"))
    sync_conn.execute(
        text("UPDATE users SET password_changed_at = COALESCE(created_at, CURRENT_TIMESTAMP) "
             "WHERE password_changed_at IS NULL")
    )


async def init_database() -> None:
    """初始化数据库并写入 seed 数据（幂等）。"""
    await create_all()
    from db.seed import seed_all

    await seed_all()


async def dispose() -> None:
    from db.base import dispose_engine, close_mongo_client

    await dispose_engine()
    await close_mongo_client()
    log.info("数据库连接已关闭")
