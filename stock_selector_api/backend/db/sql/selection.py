# db/sql/selection.py
from sqlalchemy import Float, Integer, String
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
    # 选股插件 P1 富化字段。rank 是 SQL 保留字（窗口函数），列名用 rank_no，
    # API 原样返回 rank_no，由客户端 logic/view.js 归一为 rank。
    strategy: Mapped[str] = mapped_column(String(64), default="")
    asof: Mapped[str] = mapped_column(String(16), default="")      # 信号基准交易日
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    rank_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 买入风险分（百分点）：插件的风险剔除层给的历史证据见 README《涨停与风险分》。
    # 只有"过了闸门"的票才有值，闸门关闭或历史太短无法判分时为 NULL。
    risk_ex10_pp: Mapped[float | None] = mapped_column(Float, nullable=True)
