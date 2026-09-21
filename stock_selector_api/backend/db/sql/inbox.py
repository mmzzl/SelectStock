"""db/sql/inbox.py — 站内信表（属主隔离；read_at 为空即未读）。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from db.sql.base import Base, IdMixin, TimestampMixin


class InboxMessage(IdMixin, TimestampMixin, Base):
    __tablename__ = "inbox_messages"

    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_code: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    content: Mapped[str] = mapped_column(String(2048), default="")
    read_at: Mapped[datetime | None] = mapped_column(DateTime, default=None, nullable=True)
    related_type: Mapped[str] = mapped_column(String(32), default="")
    related_id: Mapped[str] = mapped_column(String(64), default="")
