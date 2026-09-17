"""权限管理接口：权限列表与模块分组。"""

from fastapi import APIRouter, Depends

from core.dependencies import require_permission
from core.response import ok
from services import rbac_service

router = APIRouter(prefix="/sys/permissions", tags=["权限管理"])


@router.get("", summary="权限列表（可按模块过滤）", dependencies=[Depends(require_permission("sys:role:list"))])
async def list_permissions(module: str | None = None):
    return ok(await rbac_service.list_permissions(module))


@router.get("/modules", summary="权限所属模块", dependencies=[Depends(require_permission("sys:role:list"))])
async def permission_modules():
    return ok(await rbac_service.permission_modules())


@router.get("/tree", summary="权限树（模块→功能→动作，供勾选分配）", dependencies=[Depends(require_permission("sys:role:list"))])
async def permission_tree():
    return ok(await rbac_service.permission_tree())
