"""MongoDB 索引初始化：与 SQL 侧字段约束对齐（用户名唯一、外键/查询字段建索引）。"""

from db.base import get_db
from db.session import get_collection
from core.logger import log


async def ensure_indexes() -> None:
    """为所有业务集合建索引。幂等。"""
    db_name = get_db()
    log.info("开始为 MongoDB 建索引: db={db_name}", db_name=db_name)

    await get_collection("users").create_index("username", unique=True)
    await get_collection("roles").create_index("code", unique=True)
    await get_collection("permissions").create_index("code", unique=True)
    await get_collection("permissions").create_index("module")
    await get_collection("menus").create_index([("parent_id", 1)])
    await get_collection("sessions").create_index("token", unique=True)
    await get_collection("sessions").create_index("user_id")
    await get_collection("devices").create_index("name")
    await get_collection("captchas").create_index("expires_at")
    await get_collection("captchas").create_index("session_key")
    await get_collection("login_locks").create_index("lock_key", unique=True)
    await get_collection("login_locks").create_index("locked_until")
    await get_collection("password_history").create_index("user_id")
    await get_collection("password_history").create_index("created_at")
    await get_collection("user_roles").create_index("user_id")
    await get_collection("role_permissions").create_index("role_id")
    log.info("MongoDB 索引创建完成")
