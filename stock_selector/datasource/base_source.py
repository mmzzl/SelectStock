"""数据源抽象基类。

统一封装股票列表、K 线数据获取逻辑，屏蔽底层数据来源差异
（行情接口/数据库/本地文件）。策略层不用改代码即可切换数据源。
"""

from abc import ABC, abstractmethod


class BaseDataSource(ABC):
    """数据源抽象基类。"""

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    @abstractmethod
    def get_stock_list(self) -> list[dict]:
        """获取股票列表。

        Returns:
            list[dict]: 每个元素含 code / name 键。
        """
        raise NotImplementedError

    @abstractmethod
    def get_kline(self, code: str):
        """获取单只股票的标准化 K 线 DataFrame。

        返回的 DataFrame 应至少包含列：
        date / open / high / low / close / volume

        Args:
            code: 股票代码。

        Returns:
            pandas.DataFrame 或 None（无数据时）。
        """
        raise NotImplementedError
