# db/sql/ruleset.py
from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from db.sql.base import Base, IdMixin, TimestampMixin


class RuleSet(Base, IdMixin, TimestampMixin):
    """一套自定义持仓判定规则（管理页面「规则集」）。

    正文存**结构化条件**（页面填的原始形状），表达式串在写 active.json 时现拼：
    存串等于让用户在页面上写语法，而结构化条件能让校验、回放、卖点文案三边共用同一份定义。
    `sell_expr`/`add_expr` 是各条规则 OR 起来的快照串，读侧（插件判定、台账）只认这两串。

    回放状态直接挂在本行上，不另开作业表：「点应用 → 全市场回放」的产物是这一行的可信度，
    历史版本重新应用就重跑，中间过程的排队记录没有业务价值（要查进度看 Celery 的 task_id）。
    """

    __tablename__ = "rule_sets"

    name: Mapped[str] = mapped_column(String(64), nullable=False)
    remark: Mapped[str] = mapped_column(String(200), default="")
    disaster_stop_pct: Mapped[float] = mapped_column(Float, default=5.0)
    sell_conditions: Mapped[str] = mapped_column(Text, default="[]")   # JSON: [{label, conditions:[...]}]
    add_conditions: Mapped[str] = mapped_column(Text, default="[]")
    sell_expr: Mapped[str] = mapped_column(Text, default="")
    add_expr: Mapped[str] = mapped_column(Text, default="")
    used_fields: Mapped[str] = mapped_column(String(500), default="")  # 逗号分隔，列表页直接显示
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    replay_status: Mapped[str] = mapped_column(String(16), default="", index=True)  # ""/queued/running/done/failed
    replay_progress: Mapped[int] = mapped_column(Integer, default=0)
    replay_stage: Mapped[str] = mapped_column(String(128), default="")
    replay_error: Mapped[str] = mapped_column(String(512), default="")
    celery_task_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    replayed_at: Mapped[DateTime | None] = mapped_column(DateTime, nullable=True)
    posterior: Mapped[str] = mapped_column(Text, default="")           # 最近一次回放摘要 JSON
