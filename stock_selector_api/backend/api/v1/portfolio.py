"""持仓诊断契约路由（M1）：POST /api/portfolio/analyze + GET /api/portfolio/runs。

- R1-1/R1-2 判定与收益全部收敛插件，后端仅代理/落库/展示（零算术）；
- R2-3 幂等重放：同 (user_id, natural_day, holdings_md5) 复用 run_id；
- R3-1 列表按 user_id 隔离（M1 由入参/查询参数承载，鉴权收敛由 M3 补强）。
"""

from fastapi import APIRouter, Query

from core.response import ok, paged
from schemas.portfolio import AnalyzeBody
from services import portfolio_service

router = APIRouter(prefix="/portfolio", tags=["持仓诊断"])


@router.post("/analyze", summary="持仓诊断分析（代理插件，幂等重放）")
async def analyze(body: AnalyzeBody) -> dict:
    data = await portfolio_service.analyze(
        body.user_id, body.natural_day, [h.model_dump() for h in body.holdings]
    )
    return ok(data)


@router.get("/runs", summary="诊断 run 列表（按 user_id 隔离）")
async def list_runs(
    user_id: str = Query(description="属主 user_id"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=500),
) -> dict:
    items, total = await portfolio_service.list_runs(user_id, page, size)
    return ok(paged(items, total, page, size))