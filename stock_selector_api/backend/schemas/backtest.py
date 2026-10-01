"""schemas/backtest.py — 回测作业写白名单。"""
from pydantic import BaseModel, Field


class BacktestJobCreate(BaseModel):
    job_id: str = Field(min_length=1, max_length=36)
    user_id: str = Field(min_length=1, max_length=64)
    status: str = Field(default="queued", max_length=16)
    source: str = Field(default="manual", max_length=16)
    params: str = Field(default="{}", max_length=4096)
    celery_task_id: str = Field(default="", max_length=64)
    progress: int = Field(default=0, ge=0, le=100)
    stage: str = Field(default="", max_length=128)
    error: str = Field(default="", max_length=512)
    result_summary: str = Field(default="", max_length=200000)
