"""菜单管理接口：菜单 CRUD + 全量树（供管理端）。"""

from fastapi import APIRouter, Depends

from core.dependencies import require_permission
from core.response import ok
from schemas.rbac import MenuCreate, MenuUpdate
from services import rbac_service

router = APIRouter(prefix="/sys/menus", tags=["菜单管理"])


@router.get("", summary="菜单扁平列表", dependencies=[Depends(require_permission("sys:menu:list"))])
async def list_menus():
    return ok(await rbac_service.list_menu_flat())


@router.get("/tree", summary="全量菜单树（管理端）", dependencies=[Depends(require_permission("sys:menu:list"))])
async def menu_tree():
    return ok(await rbac_service.menu_tree())


@router.post("", summary="新增菜单", dependencies=[Depends(require_permission("sys:menu:create"))])
async def create_menu(body: MenuCreate):
    return ok(await rbac_service.create_menu(body.model_dump()), message="创建成功")


@router.put("/{menu_id}", summary="修改菜单", dependencies=[Depends(require_permission("sys:menu:update"))])
async def update_menu(menu_id: str, body: MenuUpdate):
    return ok(await rbac_service.update_menu(menu_id, body.model_dump(exclude_unset=True)), message="更新成功")


@router.delete("/{menu_id}", summary="删除菜单", dependencies=[Depends(require_permission("sys:menu:delete"))])
async def delete_menu(menu_id: str):
    await rbac_service.delete_menu(menu_id)
    return ok(message="删除成功")
