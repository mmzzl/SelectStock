# db/sql/backtest.py
from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from db.sql.base import Base, TimestampMixin, new_uuid


class BacktestJob(Base, TimestampMixin):
    """回测作业行：记录投递到 Celery 的任务与归并后的进度/结果摘要。

    结果明细（成交、全量净值）由内核自己写 MongoDB，这里只存精简摘要供后台列表直接渲染。
    """

    __tablename__ = "backtest_jobs"

    job_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)  # queued/running/done/failed
    source: Mapped[str] = mapped_column(String(16), default="manual")              # manual|scheduled
    params: Mapped[str] = mapped_column(Text, default="{}")                        # JSON，投递给 worker 的参数
    celery_task_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)                      # 0-100
    stage: Mapped[str] = mapped_column(String(128), default="")
    error: Mapped[str] = mapped_column(String(512), default="")
    result_summary: Mapped[str] = mapped_column(Text, default="")                  # JSON，done 后填充
