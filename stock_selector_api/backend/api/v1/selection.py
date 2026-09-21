# api/v1/selection.py
from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from core.dependencies import CurrentUserDep
from core.response import ok, paged
from services import selection_service

router = APIRouter(prefix="/selection", tags=["选股"])


class RunIn(BaseModel):
    strategies: list[str] | None = Field(default=None, max_length=8)


@router.get("/strategies", summary="可选策略")
async def strategies(user: CurrentUserDep):
    return ok(await selection_service.list_strategies())


@router.post("/run", summary="触发选股（后台任务）")
async def run(user: CurrentUserDep, body: RunIn):
    return ok(await selection_service.start_run(user.id, body.strategies), message="选股任务已创建")


@router.get("/runs", summary="选股任务列表")
async def runs(user: CurrentUserDep, page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100)):
    data = await selection_service.list_runs(user.id, page, size)
    return ok(paged(data["items"], data["total"], page, size))


@router.get("/runs/{job_id}", summary="任务详情与结果")
async def run_detail(user: CurrentUserDep, job_id: str):
    return ok(await selection_service.run_status(user.id, job_id))
