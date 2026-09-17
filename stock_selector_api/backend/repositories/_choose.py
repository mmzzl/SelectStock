"""根据 DB_BACKEND 选择 Repository 基类的开关。

用法：
    class UserRepo(_sql() or _mongo(), ...)  # 通过 make_base() 获得单继承基类
更简单：直接用 mixin 让实体 Repo 继承 _RepoBase。
"""

from config import settings


def _RepoBase():
    if settings.is_mongodb():
        from repositories.mongo_impl import MongoRepository

        return MongoRepository
    from repositories.sql_impl import SqlRepository

    return SqlRepository


def pick():
    """返回当前 DB 对应的通用 Repository 基类。"""
    return _RepoBase()
