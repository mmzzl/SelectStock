"""quote — 实时行情入口（快照解析）。"""

from .tencent import BATCH, fetch, parse, symbol

__all__ = ["BATCH", "fetch", "parse", "symbol"]
