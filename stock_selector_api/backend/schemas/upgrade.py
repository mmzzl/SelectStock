"""schemas/upgrade.py — 升级包写白名单。"""
from pydantic import BaseModel, Field


class UpgradeCreate(BaseModel):
    platform: str = Field(min_length=2, max_length=16)
    package_type: str = Field(default="", max_length=8)
    version_code: int = Field(ge=1)
    version_name: str = Field(default="", max_length=32)
    url: str = Field(default="", max_length=512)
    md5: str = Field(default="", max_length=64)
    size_bytes: int = 0
    release_notes: str = Field(default="", max_length=1024)
    forced: bool = False
    is_published: bool = False
    publish_at: str = Field(default="", max_length=32)
    created_by: str = Field(default="", max_length=64)
