"""标准化选股结果结构体。"""

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SelectResult:
    """单条标准化选股结果。

    code/name/signal/reason 四个字段是与后端、前端对接的稳定契约；
    后面的可选字段由内核统一回填，老消费方按 key 取值不受新增影响。
    """

    code: str               # 股票代码
    name: str               # 股票名称
    signal: str             # 信号类型（如趋势回踩/箱体突破）
    reason: str             # 选股理由
    extra: dict[str, Any] = field(default_factory=dict)  # 可选的扩展字段
    strategy: str = ""      # 产出该结果的策略名
    asof: str = ""          # 该信号对应的基准交易日（数据最后有效日期）
    price: float | None = None   # 基准日收盘价
    score: float | None = None   # 信号强度 0~1，用于排序；策略未给则为 None
    rank: int | None = None      # 名次：结果里有任一评分时由内核按分数降序赋值，否则保持产出顺序

    def to_dict(self) -> dict:
        """转为可 JSON 序列化的字典。"""
        return asdict(self)
