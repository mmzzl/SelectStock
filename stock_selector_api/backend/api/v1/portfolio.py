"""持仓诊断旧契约路由：一律按登录令牌解析属主（R3-1）。

App 侧请走 `/api/v1/diag/*`；这里保留给外部调度脚本，入参里的 user_id 不再被信任，
以令牌属主为准，避免任意越权读取/触发他人诊断。
"""

from fastapi import APIRouter, Query

from core.dependencies import CurrentUserDep
from core.exceptions import BizError
from core.response import ok, paged
from schemas.portfolio import AnalyzeBody
from services import diag_service, portfolio_service

router = APIRouter(prefix="/portfolio", tags=["持仓诊断"])


@router.post("/analyze", summary="持仓诊断分析（代理插件，幂等重放）")
async def analyze(user: CurrentUserDep, body: AnalyzeBody) -> dict:
    data = await portfolio_service.analyze(
        user.id, body.natural_day, [h.model_dump() for h in body.holdings]
    )
    return ok(data)


@router.get("/runs", summary="诊断 run 列表（仅当前用户）")
async def list_runs(user: CurrentUserDep, page: int = Query(1, ge=1),
                    size: int = Query(20, ge=1, le=500)) -> dict:
    items, total = await portfolio_service.list_runs(user.id, page, size)
    return ok(paged(items, total, page, size))


@router.get("/diag/{run_id}", summary="诊断运行明细（仅属主可见）")
async def diag_detail(user: CurrentUserDep, run_id: str) -> dict:
    run = await diag_service.diag_detail(run_id)
    if not run or run.get("user_id") != user.id:
        raise BizError("诊断运行不存在", code=404, status_code=404)
    return ok(run)
