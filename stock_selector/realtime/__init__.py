"""realtime —— 盘中实时链路：行情上行（redis）与策略引擎的边界。

    scripts/quote_service.py（薄 CLI）
        └─ poller.QuotePoller ──► bus（redis: rt:quote / rt:ticks / rt:heartbeat）
                                     └─► intraday 策略引擎 → risk 风控 → 告警

`session` 管「现在该不该跑」，`bus` 管「数据放哪、怎么读」，`poller` 管「怎么拉、拉不到怎么办」。
"""

from __future__ import annotations

from . import bus, session
from .poller import QuotePoller, resolve_universe, to_tick

__all__ = ["QuotePoller", "resolve_universe", "to_tick", "bus", "session"]
