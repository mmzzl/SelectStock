"""核心框架层：策略契约、插件加载器、统一调度入口。"""

from .base_strategy import BaseSelectStrategy
from .strategy_loader import StrategyLoader
from .selector import run_select

__all__ = ["BaseSelectStrategy", "StrategyLoader", "run_select"]
