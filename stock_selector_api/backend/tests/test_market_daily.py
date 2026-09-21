"""B15：盘后定时任务测试（daily_diag / daily_selection 冒烟）。

schedule_enabled=False（conftest），此处直接调任务函数。
"""
import asyncio

from scheduler.tasks import market_daily
from services import selection_service


async def test_daily_diag_returns_count():
    n = await market_daily.daily_diag(None)
    assert n >= 0


async def test_daily_selection_creates_jobs():
    n = await market_daily.daily_selection(None)
    assert n >= 0
    # 等后台 mock 任务收尾，避免在途单飞状态泄漏到后续用例
    for _ in range(100):
        if not selection_service._inflight:
            break
        await asyncio.sleep(0.05)
