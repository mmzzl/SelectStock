"""数据源层：屏蔽底层数据来源差异，统一提供股票列表与 K 线数据。

内置三种实现，通过 settings.yaml 的 datasource.type 切换，策略层代码不变：
  parquet -> 本地指标宽表（离线、含 talib 指标，回测/诊断同源）
  mongo   -> 直连 MongoDB stock_kline（行情最新，仅基础 OHLCV）
  ak      -> akshare 行情接口（需可访问外网）
"""

from .base_source import BaseDataSource
from .ak_source import AKShareSource
from .mongo_source import MongoSource
from .parquet_source import ParquetSource

__all__ = ["BaseDataSource", "AKShareSource", "MongoSource", "ParquetSource"]
