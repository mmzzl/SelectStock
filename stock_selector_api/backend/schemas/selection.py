"""schemas/selection.py — 选股任务/结果写白名单。"""
from pydantic import BaseModel, Field


class SelectJobCreate(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    strategies: str = Field(default="[]", max_length=256)
    status: str = Field(default="queued", max_length=16)
    source: str = Field(default="manual", max_length=16)
    error: str = Field(default="", max_length=512)
    result_count: int = 0


class SelectResultCreate(BaseModel):
    job_id: str = Field(min_length=1, max_length=36)
    code: str = Field(min_length=1, max_length=16)
    name: str = Field(default="", max_length=64)
    signal: str = Field(default="", max_length=64)
    reason: str = Field(default="", max_length=1024)
