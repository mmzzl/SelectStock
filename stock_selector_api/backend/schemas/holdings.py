"""schemas/holdings.py — 持仓写白名单 / API 入参 / 读模型 / 导入结果。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from schemas.common import ORMModel


class HoldingCreate(BaseModel):
    """仓储写白名单（含 user_id；对齐 PortfolioDiagRunCreate 惯例，供 _assign 落库）。"""

    user_id: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=1, max_length=16)
    name: str = Field(default="", max_length=64)
    quantity: int = Field(ge=0)
    cost_price: float = Field(ge=0)
    enabled: bool = True


class HoldingIn(BaseModel):
    """API 入参（属主由当前登录用户决定，不可客户端伪造）。"""

    code: str = Field(min_length=1, max_length=16)
    name: str = Field(default="", max_length=64)
    quantity: int = Field(ge=0)
    cost_price: float = Field(ge=0)
    enabled: bool = True


class HoldingUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=64)
    quantity: int | None = Field(default=None, ge=0)
    cost_price: float | None = Field(default=None, ge=0)
    enabled: bool | None = None


class HoldingOut(ORMModel):
    id: str
    user_id: str
    code: str
    name: str
    quantity: int
    cost_price: float
    enabled: bool


class ImportResult(BaseModel):
    total: int
    valid: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = []
    items: list[dict] = []