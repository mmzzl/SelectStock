"""用户管理接口。"""

from fastapi import APIRouter, Depends

from core.dependencies import CurrentUserDep, require_permission
from core.response import ok, paged
from schemas.common import PageParams
from schemas.user import ResetPasswordIn, UserCreate, UserUpdate
from services import rbac_service

router = APIRouter(prefix="/sys/users", tags=["用户管理"])


@router.get("", summary="用户列表", dependencies=[Depends(require_permission("sys:user:list"))])
async def list_users(q: PageParams = Depends()):
    items, total = await rbac_service.list_users(q.page, q.size, q.keyword)
    return ok(paged(items, total, q.page, q.size))


@router.post("", summary="新增用户", dependencies=[Depends(require_permission("sys:user:create"))])
async def create_user(body: UserCreate):
    data = body.model_dump()
    role_ids = data.pop("role_ids", [])
    created = await rbac_service.create_user(data)
    if role_ids:
        from repositories import users

        await users.set_roles(created["id"], role_ids)
    return ok(created, message="创建成功")


@router.put("/{user_id}", summary="修改用户", dependencies=[Depends(require_permission("sys:user:update"))])
async def update_user(user_id: str, body: UserUpdate, operator: CurrentUserDep):
    updated = await rbac_service.update_user(user_id, body.model_dump(exclude_unset=True), operator)
    return ok(updated, message="更新成功")


@router.delete("/{user_id}", summary="删除用户", dependencies=[Depends(require_permission("sys:user:delete"))])
async def delete_user(user_id: str, operator: CurrentUserDep):
    await rbac_service.delete_user(user_id, operator)
    return ok(message="删除成功")


@router.post(
    "/{user_id}/password",
    summary="重置密码（管理员）",
    dependencies=[Depends(require_permission("sys:user:update"))],
)
async def reset_password(user_id: str, body: ResetPasswordIn):
    await rbac_service.reset_password(user_id, body.new_password)
    return ok(message="密码已重置")
