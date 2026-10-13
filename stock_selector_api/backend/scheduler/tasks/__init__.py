# 任务模块在此显式导入以随包加载（loader 亦支持按名延迟 import）
from scheduler.tasks import device_health  # noqa: F401
from scheduler.tasks import market_daily  # noqa: F401
from scheduler.tasks import news_daily  # noqa: F401
