"""统一日志层：loguru，控制台 + 滚动文件双输出。"""

import sys
from pathlib import Path

from loguru import logger

from config import settings

_LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)


def setup_logging() -> None:
    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    logger.remove()  # 清掉默认 handler，避免控制台重复输出
    logger.add(
        sys.stderr,
        level=settings.log_level,
        format=_LOG_FORMAT,
        enqueue=True,
        backtrace=settings.debug,
        diagnose=settings.debug,
    )
    logger.add(
        log_dir / "app_{time:YYYY-MM-DD}.log",
        level=settings.log_level,
        format=_LOG_FORMAT,
        rotation="00:00",      # 每天 0 点滚动
        retention="30 days",   # 保留 30 天
        compression="gz",
        encoding="utf-8",
        enqueue=True,
    )
    logger.info("日志初始化完成: level={}, dir={log_dir}", settings.log_level, log_dir=log_dir)


# 供全项目统一引用的实例
log = logger
