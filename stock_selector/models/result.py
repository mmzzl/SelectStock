"""标准化选股结果结构体。"""

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SelectResult:
    """单条标准化选股结果。

    所有策略统一输出 code/name/signal/reason 四个字段，
    保证前端、Go、Java 等跨系统对接格式一致。
    """

    code: str               # 股票代码
    name: str               # 股票名称
    signal: str             # 信号类型（如趋势回踩/箱体突破）
    reason: str             # 选股理由
    extra: dict[str, Any] = field(default_factory=dict)  # 可选的扩展字段

    def to_dict(self) -> dict:
        """转为可 JSON 序列化的字典。"""
        return asdict(self)
