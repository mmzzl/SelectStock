"""db/sql/holdings.py — 用户持仓表（REQ-018）。归属 user_id，隔离红线 R3-1。"""
from __future__ import annotations

from sqlalchemy import Boolean, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db.sql.base import Base, IdMixin, TimestampMixin


class Holding(IdMixin, TimestampMixin, Base):
    __tablename__ = "holdings"
    __table_args__ = (
        UniqueConstraint("user_id", "code", name="uq_holdings_user_code"),
    )

    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(64), default="")
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)