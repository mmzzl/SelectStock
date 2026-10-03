"""api/external.py — 指定日期的诊断入口（补跑/回补用）。

属主取自登录令牌，入参不再接受 user_id：公网暴露下任何凭 user_id 触发/读取他人都能越权。
"""

from __future__ import annotations

from fastapi import APIRouter

from core.dependencies import CurrentUserDep
from core.exceptions import BizError
from core.response import ok
from pydantic import BaseModel, Field
from services import diag_service

router = APIRouter(prefix="/external/portfolio", tags=["外部调度"])


class DiagIn(BaseModel):
    natural_day: str = Field(min_length=8, max_length=16)


@router.post("/diag", summary="按日期触发持仓诊断（幂等批次，仅当前用户）")
async def external_diag(user: CurrentUserDep, body: DiagIn):
    out = await diag_service.trigger_diag(user.id, body.natural_day)
    if out is None:
        raise BizError("该用户无启用持仓，跳过诊断", code=204, status_code=204)
    return ok(out)
