# db/sql/selection.py
from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from db.sql.base import Base, IdMixin, TimestampMixin, new_uuid


class SelectJob(Base, TimestampMixin):
    __tablename__ = "select_jobs"

    job_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    strategies: Mapped[str] = mapped_column(String(256), default="")   # JSON 数组字符串
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)  # queued/running/done/failed
    source: Mapped[str] = mapped_column(String(16), default="manual")  # manual|scheduled
    error: Mapped[str] = mapped_column(String(512), default="")
    result_count: Mapped[int] = mapped_column(Integer, default=0)


class SelectResult(Base, IdMixin, TimestampMixin):
    __tablename__ = "select_results"

    job_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(64), default="")
    signal: Mapped[str] = mapped_column(String(64), default="")
    reason: Mapped[str] = mapped_column(String(1024), default="")
