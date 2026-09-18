"""api/external.py — 外部系统契约入口（M3 外部调度）。单元鉴权在 M5/M4 收敛前保持"可调用"。"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from core.exceptions import BizError
from core.response import ok
from services import diag_service

router = APIRouter(prefix="/external/portfolio", tags=["外部调度"])


class DiagIn(BaseModel):
    user_id: str | None = Field(default=None, min_length=1, max_length=64)
    natural_day: str = Field(min_length=8, max_length=16)


@router.post("/diag", summary="外部调度持仓诊断（幂等批次）")
async def external_diag(body: DiagIn):
    out = await diag_service.trigger_diag(body.user_id, body.natural_day)
    if out is None:
        raise BizError("该用户无启用持仓，跳过诊断", code=204, status_code=204)
    return ok(out)