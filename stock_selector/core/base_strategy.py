"""策略插件统一抽象契约（基类）。

所有选股策略必须继承 BaseSelectStrategy 并实现 run(df) 方法。
框架不依赖具体策略，实现依赖倒置，彻底解耦。
"""

from abc import ABC, abstractmethod


class BaseSelectStrategy(ABC):
    """选股策略基类，定义插件统一规范。

    约束：
    - 必须实现 run(df) 方法
    - 统一输入：标准化 K 线 DataFrame（含 date/open/high/low/close/volume 列）
    - 统一输出：list[SelectResult]（code/name/signal/reason 结构化结果）

    子类可选覆盖：
    - name(): 策略名称（默认取类名转蛇形）
    - description: 策略描述
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

    @abstractmethod
    def run(self, df):
        """执行选股逻辑。

        Args:
            df: 标准化 K 线 DataFrame，字段含 date/open/high/low/close/volume，
                date 索引或列为日期。

        Returns:
            list[SelectResult]: 命中该策略的选股结果列表。
        """
        raise NotImplementedError
