"""schemas/ruleset.py — 规则集写白名单（repository 的 mass-assignment 防线）。"""
from datetime import datetime

from pydantic import BaseModel, Field


class RuleSetCreate(BaseModel):
    """一行规则集。`sell_conditions`/`add_conditions` 存结构化条件的 JSON 串，
    `sell_expr`/`add_expr` 由服务层用 rule_expr 拼好并校验后写入，页面不直接提交这两串。"""

    name: str = Field(min_length=1, max_length=64)
    remark: str = Field(default="", max_length=200)
    disaster_stop_pct: float = Field(default=8.0, ge=1.0, le=30.0)
    sell_conditions: str = Field(default="[]", max_length=20000)
    add_conditions: str = Field(default="[]", max_length=20000)
    sell_expr: str = Field(default="", max_length=2000)
    add_expr: str = Field(default="", max_length=2000)
    used_fields: str = Field(default="", max_length=500)
    is_active: bool = False
    replay_status: str = Field(default="", max_length=16)
    replay_progress: int = Field(default=0, ge=0, le=100)
    replay_stage: str = Field(default="", max_length=128)
    replay_error: str = Field(default="", max_length=512)
    celery_task_id: str = Field(default="", max_length=64)
    replayed_at: datetime | None = None
    posterior: str = Field(default="", max_length=200000)
