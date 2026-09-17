"""设备（示例模块）接口：演示「新增一个功能」的标准写法。

核心：每个接口一行权限依赖 `Depends(require_permission("device:xxx"))`。
"""

from fastapi import APIRouter, Depends

from core.dependencies import require_permission
from core.response import ok, paged
from schemas.common import PageParams
from schemas.device import DeviceCreate, DeviceUpdate
from services import device_service

router = APIRouter(prefix="/devices", tags=["设备管理（示例）"])


@router.get("", summary="设备列表", dependencies=[Depends(require_permission("device:list"))])
async def list_devices(status: str | None = None, q: PageParams = Depends()):
    items, total = await device_service.list_devices(q.page, q.size, q.keyword, status)
    return ok(paged(items, total, q.page, q.size))


@router.get("/summary", summary="设备统计", dependencies=[Depends(require_permission("device:list"))])
async def device_summary():
    return ok(await device_service.summary())


@router.get("/{device_id}", summary="设备详情", dependencies=[Depends(require_permission("device:list"))])
async def get_device(device_id: str):
    return ok(await device_service.get_device(device_id))


@router.post("", summary="新增设备", dependencies=[Depends(require_permission("device:create"))])
async def create_device(body: DeviceCreate):
    return ok(await device_service.create_device(body.model_dump()), message="创建成功")


@router.put("/{device_id}", summary="修改设备", dependencies=[Depends(require_permission("device:update"))])
async def update_device(device_id: str, body: DeviceUpdate):
    return ok(await device_service.update_device(device_id, body.model_dump(exclude_unset=True)), message="更新成功")


@router.delete("/{device_id}", summary="删除设备", dependencies=[Depends(require_permission("device:delete"))])
async def delete_device(device_id: str):
    await device_service.delete_device(device_id)
    return ok(message="删除成功")
