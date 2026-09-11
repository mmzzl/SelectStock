"""回测引擎插件包：模拟账户、指标、引擎、数据结构。"""

from stock_selector.backtest.broker import Broker, Position, Trade

__all__ = ["Broker", "Position", "Trade"]
