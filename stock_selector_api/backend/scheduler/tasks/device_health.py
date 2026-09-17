"""示例定时任务：设备状态巡检（演示如何在模板里加定时任务）。

新增任务三步：
1. 在本目录新建 module.py，写 async def func(item)
2. 在 conf/inputs.conf 加一节 [task://module:func] 配置 interval/cron
3. 重启服务即生效，无需改代码
"""

from core.logger import log
from scheduler.loader import ScheduleItem


async def device_health_check(item: ScheduleItem) -> None:
    """示例：统计设备总数并打日志（演示用，不放真实业务）。"""
    try:
        from repositories import devices

        total = await devices.count()
        by_status = await devices.status_count()
        log.info("定时任务[{}] 设备巡检: 总数={} 状态分布={}",
                 item.title, total, by_status)
    except Exception:
        log.exception("设备巡检任务异常")
