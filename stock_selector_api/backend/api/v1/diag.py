"""api/v1/diag.py — 持仓诊断（属主隔离）：手动刷新当日诊断 + 历史回看。

与 18:00 定时任务走同一条 `diag_service.trigger_diag`：同一天 10 分钟内重复点按钮命中幂等批次，
只重放旧结果；超过 10 分钟才原地重跑刷新行情。判定与收益全部由插件返回，后端零算术（R1-2）。
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Query

from core.dependencies import CurrentUserDep
from core.exceptions import BizError
from core.response import ok, paged
from services import diag_service, portfolio_service

router = APIRouter(prefix="/diag", tags=["持仓诊断"])


@router.post("/run", summary="刷新当日持仓诊断（10 分钟内重放旧结果，不重复计算）")
async def run_now(user: CurrentUserDep):
    out = await diag_service.trigger_diag(user.id, date.today().isoformat())
    if out is None:
        raise BizError("还没有启用中的持仓，先添加持仓或导入交割单", code=409, status_code=409)
    started = out.get("status") in ("queued", "running")
    return ok(out, message="诊断已启动" if started else "结果不到 10 分钟，10 分钟后再点即可刷新")


@router.get("/runs", summary="诊断记录列表")
async def runs(user: CurrentUserDep, page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100)):
    items, total = await portfolio_service.list_runs(user.id, page, size)
    return ok(paged(items, total, page, size))

@router.get("/runs/{run_id}", summary="诊断明细（仅属主可见）")
async def run_detail(user: CurrentUserDep, run_id: str):
    run = await diag_service.diag_detail(run_id)
    if not run or run.get("user_id") != user.id:
        raise BizError("诊断运行不存在", code=404, status_code=404)
    return ok(run)
