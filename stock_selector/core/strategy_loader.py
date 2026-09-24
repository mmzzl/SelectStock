"""动态插件加载器。

运行时扫描 strategies 目录下所有策略文件，动态导入、校验契约、生成策略注册表。
新增策略无需手动 import，零配置注册。
"""

import importlib
import inspect
import logging
import pkgutil
from collections import OrderedDict

from .base_strategy import BaseSelectStrategy
from .. import strategies as strategies_pkg

logger = logging.getLogger("stock_selector")


class StrategyLoader:
    """扫描并加载所有策略插件的加载器。"""

    def __init__(self, strategy_params: dict | None = None):
        self._params = strategy_params or {}
        self._registry: OrderedDict[str, type[BaseSelectStrategy]] = OrderedDict()

    def load_all(self) -> "StrategyLoader":
        """扫描 strategies 包下所有模块，注册满足契约的策略类。"""
        for mod_info in pkgutil.iter_modules(strategies_pkg.__path__):
            module = importlib.import_module(f"{strategies_pkg.__name__}.{mod_info.name}")
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if self._is_valid_strategy(obj):
                    # 探测实例仅用于取策略名，参数由 create() 统一注入
                    self._registry[obj().name] = obj
        return self

    def _is_valid_strategy(self, cls) -> bool:
        """校验是否为有效策略类：继承基类、非抽象、且至少实现了一侧判据。

        只继承不实现任何判据的类会被跳过并告警——否则它会以"永远 0 命中"的
        形式混进策略列表，故障要到线上才暴露。
        """
        if (
            not issubclass(cls, BaseSelectStrategy)
            or cls is BaseSelectStrategy
            or inspect.isabstract(cls)
        ):
            return False
        implements = (
            cls.run is not BaseSelectStrategy.run
            or cls.signal_at is not BaseSelectStrategy.signal_at
        )
        if not implements:
            logger.warning("策略 %s 未实现 signal_at()/run()，跳过注册", cls.__name__)
        return implements

    def names(self) -> list[str]:
        """返回所有已注册策略名称。"""
        return list(self._registry.keys())

    def create(self, name: str) -> BaseSelectStrategy:
        """按名称创建策略实例。"""
        name = name.strip().lower()
        cls = self._registry.get(name)
        if cls is None:
            raise KeyError(f"未找到策略插件: {name}，可用: {', '.join(self.names())}")
        return cls(config=self._params.get(name, None))

    def describe(self) -> list[dict]:
        """各策略的自述元数据，供接口向前端暴露中文名与所需数据。"""
        out = []
        for name in self.names():
            inst = self.create(name)
            out.append({
                "name": name,
                "description": inst.description,
                "as_of": type(inst).signal_at is not BaseSelectStrategy.signal_at,
                "required_columns": inst.required_columns(),
                "required_rows": inst.required_rows(),
            })
        return out
