"""Celery 应用：回测任务的投递与进度查询。

红线：FastAPI 进程只用它 send_task / 读 AsyncResult，回测计算全部发生在 worker。
include 是惰性加载的，后端进程不会因此 import 内核依赖。
"""
from __future__ import annotations

from celery import Celery

from config import settings

celery_app = Celery(
    "selectstock",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["tasks.backtest_tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Shanghai",
    enable_utc=True,
    # 不开 track_started 时，排队中的任务查询结果一律是 PENDING，前端分不清排队与未投递
    task_track_started=True,
    result_expires=settings.celery_result_ttl,
    # 回测要整天扫 parquet，一次只预取一个任务，避免并发把内存吃满
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
)
