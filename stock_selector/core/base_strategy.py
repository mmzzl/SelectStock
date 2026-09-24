"""策略插件统一抽象契约（基类）。

所有选股策略必须继承 BaseSelectStrategy，并实现 as-of 判据 signal_at(df)
（推荐，可与回测逐日复用）或整体判据 run(df)。框架不依赖具体策略，实现依赖倒置。
"""

import warnings
from abc import ABC

# 任何策略都需要的最小列集；数据源剪列时按各策略声明取并集
BASE_COLUMNS = ("date", "open", "high", "low", "close", "volume")


class BaseSelectStrategy(ABC):
    """选股策略基类，定义插件统一规范。

    两种实现方式（必须覆盖其中之一，否则加载时告警并跳过注册）：

    - signal_at(df) -> SelectResult | None
        **as-of 判据**：只依据传入 df 的数据判断"截至该 df 最后一行"是否出信号。
        不感知日期，也不访问 df 之外的数据 —— 这是选股与回测同源的支点：
        实盘把整段历史传进来等价于 as-of 到最新交易日，回测则逐日切片传进来。
    - run(df) -> list[SelectResult]
        整体判据，默认实现就是 signal_at 的列表化。需要一次产出多条结果
        （如同一股票多个信号）或跨期比较时才覆盖它。

    可选覆盖：
    - required_columns(): 策略需要读取的列，内核据此向数据源补齐剪列清单
    - required_rows():    策略需要的最少回溯行数
    - name:               策略标识（默认类名蛇形）
    """

    def __init__(self, config: dict | None = None):
        # 策略参数配置（来自 settings.yaml 的 strategy_params.<name>）
        self.config = config or {}

    @property
    def name(self) -> str:
        """策略唯一标识名称，用于插件注册和按名调度。"""
        cls = type(self).__name__
        return cls[0].lower() + "".join(
            f"_{c.lower()}" if c.isupper() else c for c in cls[1:]
        )

    @property
    def description(self) -> str:
        return ""

    def required_columns(self) -> list[str]:
        return list(BASE_COLUMNS)

    def required_rows(self) -> int:
        """策略判据所需的最少回溯行数（含基准日）。"""
        return 120

    def signal_at(self, df):
        """as-of 判据：命中返回 SelectResult，未命中返回 None。

        Args:
            df: 截至基准日的标准化日线，date 升序，最后一行即基准日。
        """
        return None

    def run(self, df) -> list:
        """执行选股，返回命中结果列表。

        默认把 as-of 判据列表化，因此只实现 signal_at 的策略在实盘选股与回测中
        走的是同一段代码。
        """
        if type(self).signal_at is BaseSelectStrategy.signal_at:
            warnings.warn(
                f"{type(self).__name__} 未实现 signal_at() 或 run()，不会产出任何选股结果",
                RuntimeWarning,
                stacklevel=2,
            )
            return []
        hit = self.signal_at(df)
        return [hit] if hit is not None else []
