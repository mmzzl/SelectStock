"""盘后定时任务：消息面摘要推送（钉钉 + 站内信）。由 inputs.conf 声明触发。

排在 `analyze_news_llm.py`（根调度器 22:00）之后——早一步跑就是在推上一晚的旧批次。
"""
from __future__ import annotations

from core.logger import log
from services import news_service


async def daily_news_digest(item) -> dict:
    out = await news_service.push_digest()
    log.info("daily_news_digest 完成：day={} 机器人={} 站内信={}",
             out.get("day"), len(out.get("robots") or []), len(out.get("users") or []))
    return out
