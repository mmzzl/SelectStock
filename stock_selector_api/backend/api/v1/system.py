# api/v1/system.py
"""系统监控接口：CPU / 内存 / 磁盘使用率只读采样。"""
import asyncio

from fastapi import APIRouter, Depends

from core.dependencies import CurrentUserDep, require_permission
from core.response import ok
from services import system_service

router = APIRouter(prefix="/system", tags=["系统监控"])


@router.get("/metrics", summary="CPU/内存/磁盘使用率",
            dependencies=[Depends(require_permission("sys:monitor:list"))])
async def metrics(user: CurrentUserDep):
    # cpu_percent 带采样窗口，是阻塞调用，放线程里跑
    return ok(await asyncio.to_thread(system_service.collect))
