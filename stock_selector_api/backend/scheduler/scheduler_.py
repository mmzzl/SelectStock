"""APScheduler 封装：根据 inputs.conf 解析结果注册周期任务。"""

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config import settings
from core.logger import log
from scheduler.loader import parse_inputs_conf, ScheduleItem
from scheduler import tasks  # noqa: F401  确保任务模块注册


_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    return _scheduler


def _resolve_target(item: ScheduleItem):
    """task://module:func → 可调用对象；script:// 暂不执行（外部脚本需独立进程）。"""
    if item.kind not in ("task",):
        log.warning("暂不支持自动执行该类型任务: {}", item.section)
        return None
    if ":" not in item.target:
        log.warning("task:// 需形如 module:func，忽略: {}", item.target)
        return None
    mod_path, func_name = item.target.split(":", 1)
    from importlib import import_module

    try:
        mod = import_module(f"scheduler.tasks.{mod_path}")
        return getattr(mod, func_name)
    except (ImportError, AttributeError) as e:
        log.warning("任务加载失败 {}: {}", item.target, e)
        return None


def _build_trigger(item: ScheduleItem):
    if item.interval:
        return IntervalTrigger(seconds=item.interval)
    if item.cron_kwargs:
        return CronTrigger(**item.cron_kwargs)
    log.warning("任务 {} 未配置 interval/cron，默认每小时执行", item.section)
    return IntervalTrigger(seconds=3600)


async def _task_wrapper(func, item: ScheduleItem):
    try:
        await func(item)
    except Exception as e:
        log.exception("定时任务执行失败: {}", item.title)
        # 订阅策略「task_failed」事件：任务失败发通知（失败不影响调度，逐任务写日志）
        try:
            from services import notify_service

            await notify_service.emit("task_failed", job=item.section, error=str(e) or "未知异常")
        except Exception:
            log.exception("任务失败通知发送异常")


def setup_scheduler() -> AsyncIOScheduler:
    """按 inputs.conf 注册全部任务并启动。返回 scheduler 实例。"""
    sched = get_scheduler()
    if sched.running:
        return sched

    items = parse_inputs_conf(settings.schedule_conf)
    registered = 0
    for item in items:
        if not item.enable:
            log.info("跳过已禁用任务: {}", item.section)
            continue
        func = _resolve_target(item)
        if func is None:
            continue
        trigger = _build_trigger(item)
        sched.add_job(
            _task_wrapper,
            trigger=trigger,
            args=[func, item],
            id=item.section,
            replace_existing=True,
            misfire_grace_time=60,
        )
        registered += 1
        log.info("已注册定时任务 [{}] title={} trigger={}", item.kind, item.title, trigger)

    if registered:
        sched.start()
        log.info("调度器启动，共注册任务 {} 个", registered)
    else:
        log.info("无可用定时任务，调度器不启动")
    return sched


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("调度器已停止")
    _scheduler = None
