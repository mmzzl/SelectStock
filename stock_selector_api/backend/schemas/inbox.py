"""schemas/inbox.py — 站内信写白名单 / 读模型。"""
from datetime import datetime

from pydantic import BaseModel, Field


class InboxCreate(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    event_code: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=256)
    content: str = Field(default="", max_length=2048)
    read_at: datetime | None = None
    related_type: str = Field(default="", max_length=32)
    related_id: str = Field(default="", max_length=64)


class InboxOut(BaseModel):
    id: str
    user_id: str
    event_code: str
    title: str
    content: str = ""
    read_at: datetime | None = None
    created_at: datetime | None = None
    related_type: str = ""
    related_id: str = ""
