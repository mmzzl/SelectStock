"""密码历史模型：记录每人最近 N 个旧密码哈希，改密时拒绝重用。"""

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from db.sql.base import Base, IdMixin


class PasswordHistory(Base, IdMixin):
    __tablename__ = "password_history"

    user_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True, nullable=False)
