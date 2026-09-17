"""角色管理接口：角色 CRUD + 权限分配。"""

from fastapi import APIRouter, Depends

from core.dependencies import require_permission
from core.response import ok
from schemas.rbac import RoleCreate, RoleUpdate, RoleAssignPermissions
from services import rbac_service

router = APIRouter(prefix="/sys/roles", tags=["角色管理"])


@router.get("", summary="角色列表", dependencies=[Depends(require_permission("sys:role:list"))])
async def list_roles():
    return ok(await rbac_service.list_roles())


@router.post("", summary="新增角色", dependencies=[Depends(require_permission("sys:role:create"))])
async def create_role(body: RoleCreate):
    return ok(await rbac_service.create_role(body.model_dump()), message="创建成功")


@router.put("/{role_id}", summary="修改角色", dependencies=[Depends(require_permission("sys:role:update"))])
async def update_role(role_id: str, body: RoleUpdate):
    return ok(await rbac_service.update_role(role_id, body.model_dump(exclude_unset=True)), message="更新成功")


@router.delete("/{role_id}", summary="删除角色", dependencies=[Depends(require_permission("sys:role:delete"))])
async def delete_role(role_id: str):
    await rbac_service.delete_role(role_id)
    return ok(message="删除成功")


@router.post("/{role_id}/permissions", summary="为角色分配权限", dependencies=[Depends(require_permission("sys:role:update"))])
async def assign_permissions(role_id: str, body: RoleAssignPermissions):
    await rbac_service.assign_permissions(role_id, body.permission_ids)
    return ok(message="分配成功")


@router.get("/{role_id}", summary="角色详情（含已分配权限，供回显）", dependencies=[Depends(require_permission("sys:role:list"))])
async def get_role(role_id: str):
    return ok(await rbac_service.get_role(role_id))
