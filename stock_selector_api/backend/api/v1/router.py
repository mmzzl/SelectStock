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
    holdings,
    diag,
    trades,
    messages,
    selection,
    sector,
    news,
    backtest,
    system,
    upgrade,
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

# —— 持仓模块 ——
api_router.include_router(holdings.router, dependencies=_password_gate)

# —— 持仓诊断模块 ——
api_router.include_router(diag.router, dependencies=_password_gate)

# —— 交易记录 / 交割单导入模块 ——
api_router.include_router(trades.router, dependencies=_password_gate)

# —— 站内信模块 ——
api_router.include_router(messages.router, dependencies=_password_gate)

# —— 选股模块 ——
api_router.include_router(selection.router, dependencies=_password_gate)

# —— 板块热力图模块（预计算结果薄代理）——
api_router.include_router(sector.router, dependencies=_password_gate)

# —— 消息面模块（新闻利空/风险薄代理 + 持仓相关标记）——
api_router.include_router(news.router, dependencies=_password_gate)

# —— 回测模块（Celery 投递）与系统监控 ——
api_router.include_router(backtest.router, dependencies=_password_gate)
api_router.include_router(system.router, dependencies=_password_gate)

# —— 版本升级模块（超管） ——
api_router.include_router(upgrade.router, dependencies=_password_gate)

# 新增模块后追加：api_router.include_router(your_module_router)
