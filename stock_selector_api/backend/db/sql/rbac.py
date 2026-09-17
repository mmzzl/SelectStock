"""角色模型、权限模型、用户-角色-权限关联表、菜单模型、会话模型。"""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Table
from sqlalchemy.orm import Mapped, mapped_column

from db.sql.base import Base, IdMixin, TimestampMixin


class Role(Base, IdMixin, TimestampMixin):
    __tablename__ = "roles"

    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    description: Mapped[str] = mapped_column(String(255), default="")


class Permission(Base, IdMixin, TimestampMixin):
    __tablename__ = "permissions"

    code: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    module: Mapped[str] = mapped_column(String(64), default="", index=True)  # 归属模块，便于按模块分组


# ---- 关联表 ----
user_roles = Table(
    "user_roles",
    Base.metadata,
    Column("user_id", String(36), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("role_id", String(36), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
)

role_permissions = Table(
    "role_permissions",
    Base.metadata,
    Column("role_id", String(36), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
    Column("permission_id", String(36), ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True),
)


class Menu(Base, IdMixin, TimestampMixin):
    __tablename__ = "menus"

    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # 菜单归属类型：dir(目录) | menu(菜单) | button(按钮)
    menu_type: Mapped[str] = mapped_column(String(16), default="menu")
    # 前端路由路径（page 对应 frontend/static/pages/xxx.html，url 为完整路径）
    path: Mapped[str] = mapped_column(String(128), default="")
    icon: Mapped[str] = mapped_column(String(64), default="")
    parent_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # 可见所需权限码（逗号分隔，任一满足即可）
    permission_code: Mapped[str] = mapped_column(String(128), default="")
    is_visible: Mapped[bool] = mapped_column(default=True)


class Session(Base, IdMixin):
    """Session 认证会话（AUTH_MODE=session 时使用）。"""

    __tablename__ = "sessions"

    user_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    token: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
