"""v1 路由汇总：新增业务模块后，在这里挂上对应 router。"""

from fastapi import APIRouter, Depends

from api.v1 import (
    auth,
    sys_users,
    sys_roles,
    sys_menus,
    sys_permissions,
    notify,
    devices,
)
from core.dependencies import ensure_password_current

api_router = APIRouter()

# —— 系统基础（认证路由豁免密码到期网关：登录/改密/登出/me/menus 常开）——
api_router.include_router(auth.router)

# —— 业务路由：统一挂密码到期网关（密码过期 → 428，改密后恢复）——
_password_gate = [Depends(ensure_password_current)]
api_router.include_router(sys_users.router, dependencies=_password_gate)
api_router.include_router(sys_roles.router, dependencies=_password_gate)
api_router.include_router(sys_menus.router, dependencies=_password_gate)
api_router.include_router(sys_permissions.router, dependencies=_password_gate)
api_router.include_router(notify.router, dependencies=_password_gate)

# —— 示例业务模块 ——
api_router.include_router(devices.router, dependencies=_password_gate)

# 新增模块后追加：api_router.include_router(your_module_router)
