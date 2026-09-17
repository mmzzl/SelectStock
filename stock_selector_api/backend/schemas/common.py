"""Pydantic schema 通用约定：分页、时间戳、实体公共字段。"""

from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ORMModel(BaseModel):
    """ORM/文档 → dict 后进出的基类；dict 可直接传给 model_validate。"""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class PageParams(BaseModel):
    page: int = Field(1, ge=1)
    size: int = Field(20, ge=1, le=500)
    keyword: str | None = None


class PageResult(BaseModel, Generic[T]):
    items: list[T] = []
    total: int = 0
    page: int = 1
    size: int = 20
