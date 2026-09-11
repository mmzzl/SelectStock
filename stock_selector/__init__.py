"""插件化量化选股系统核心包。

提供统一的插件化量化选股内核，支持命令行、模块导入、HTTP 接口三种运行方式。
对外唯一入口函数为 run_select()，保证多端结果一致。
"""

from .core.selector import run_select

__all__ = ["run_select"]
__version__ = "0.1.0"
