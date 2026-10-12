"""盘中计算层：分钟条 → 指标 → 信号 → 风控 → 决策落盘。

与盘后选股内核（`core/selector.py`）的分工是**周期**而不是口径：这里在分钟条上产信号，
过的是同一个 `risk.RiskEngine`，用的同一套 talib 参数（见 `indicators.py`）。

模块划分：`bars` 把累计量快照还原成分钟 OHLCV，`indicators` 算分钟指标，`signals` 是三类
策略规则（技术面/盘口/基本面事件），`engine` 串成循环并调风控，`store` 落复盘文件，
`events` 供当日公告/新闻拆出的多空目标。

用法（常驻进程见 scripts/intraday_engine.py）：

    from stock_selector.intraday import EngineConfig, IntradayEngine
    from stock_selector.risk import RiskEngine

    engine = IntradayEngine(client, ["600000"], RiskEngine.from_settings(settings))
    engine.drain()          # 读一批 tick，产决策；返回本轮统计
"""

from .bars import BarBuilder
from .engine import EngineConfig, IntradayEngine, book_ratio
from .signals import Signal, SignalConfig, View, evaluate

__all__ = ["BarBuilder", "EngineConfig", "IntradayEngine", "book_ratio",
           "Signal", "SignalConfig", "View", "evaluate"]
