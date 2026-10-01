# api/v1/backtest.py
"""回测接口：参数校验后投递 Celery，读回作业状态。后端不计算。"""
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from core.dependencies import CurrentUserDep, require_permission
from core.response import ok, paged
from services import backtest_service

router = APIRouter(prefix="/backtest", tags=["回测"])


class RunIn(BaseModel):
    # 原样透传参数字典，白名单与区间由 core.backtest_params 统一校验
    params: dict | None = None


@router.get("/options", summary="表单初始化（可选策略 + 参数缺省与区间）",
            dependencies=[Depends(require_permission("backtest:list"))])
async def options(user: CurrentUserDep):
    return ok(backtest_service.options())


@router.post("/run", summary="触发回测（投递 Celery worker）",
            dependencies=[Depends(require_permission("backtest:run"))])
async def run(user: CurrentUserDep, body: RunIn):
    data = await backtest_service.start_run(user.id, body.params)
    return ok(data, message="回测任务已投递")


@router.get("/workers", summary="Celery worker 在线情况",
            dependencies=[Depends(require_permission("backtest:list"))])
async def workers(user: CurrentUserDep):
    return ok(await backtest_service.worker_status())


@router.get("/runs", summary="回测作业列表",
            dependencies=[Depends(require_permission("backtest:list"))])
async def runs(user: CurrentUserDep, page: int = Query(1, ge=1),
               size: int = Query(20, ge=1, le=100)):
    data = await backtest_service.list_runs(user.id, page, size)
    return ok(paged(data["items"], data["total"], page, size))


@router.get("/runs/{job_id}", summary="作业详情（含净值曲线与最近成交）",
            dependencies=[Depends(require_permission("backtest:list"))])
async def run_detail(user: CurrentUserDep, job_id: str):
    return ok(await backtest_service.job_detail(user.id, job_id))
