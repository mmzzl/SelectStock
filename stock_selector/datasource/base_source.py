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

    def name_of(self, code: str) -> str:
        """股票名称兜底：股票列表未携带名称时，由数据源在取数后提供。"""
        return ""

    def max_date(self) -> str | None:
        """本次取数覆盖到的最晚交易日，用于向调用方暴露数据新鲜度。"""
        return None

    def prepare(self, columns: list[str] | None = None, rows: int = 0) -> None:
        """按策略声明补齐取数需求（只放宽、不收紧）。

        内核在遍历股票前调用它，把各策略 required_columns/required_rows 的并集
        下发给数据源。不支持剪列的数据源无需覆盖。
        """

    def close(self) -> None:
        """释放底层连接；无连接的数据源无需覆盖。"""
