"""账户锁定模型（暴力破解防护）。

按 用户名(user:xxx) 与 客户端IP(ip:xxx) 两个维度独立计数/锁定；
登录失败与验证码失败共用一个计数器与锁定状态，阈值分别配置。
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from db.sql.base import Base, IdMixin


class LoginLock(Base, IdMixin):
    __tablename__ = "login_locks"

    lock_key: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)  # user:xxx / ip:xxx
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
