"""盘后定时任务：持仓诊断 + 选股（M4）。由 inputs.conf 声明触发。"""
from __future__ import annotations

from datetime import date

from core.logger import log
from repositories import users
from services import diag_service, selection_service


def _today() -> str:
    return date.today().isoformat()


async def daily_diag(item) -> int:
    """对所有启用用户触发当日持仓诊断（无持仓自动跳过）。"""
    rows, _ = await users.list(filters={"is_active": True}, page=1, size=500)
    n = 0
    for u in rows or []:
        try:
            out = await diag_service.trigger_diag(u["id"], _today())
            if out:
                n += 1
        except Exception as e:   # noqa: BLE001
            log.warning("daily_diag 跳过用户 {}: {}", u.get("username"), e)
    log.info("daily_diag 完成，处理 {} 个用户", n)
    return n


async def daily_selection(item) -> int:
    """对每个启用用户触发默认策略选股（source=scheduled）。"""
    rows, _ = await users.list(filters={"is_active": True}, page=1, size=500)
    n = 0
    for u in rows or []:
        try:
            await selection_service.start_run(u["id"], None, source="scheduled")
            n += 1
        except Exception as e:   # noqa: BLE001
            log.warning("daily_selection 跳过用户 {}: {}", u.get("username"), e)
    log.info("daily_selection 完成，创建 {} 个任务", n)
    return n
