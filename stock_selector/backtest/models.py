"""回测数据结构：订单、指令、净值点、结果。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Order:
    """买入候选（策略建议的目标金额）。"""

    code: str
    amount: float
    name: str = ""


@dataclass
class Instruction:
    """持仓状态迁移指令（sell/add）。"""

    side: str  # sell | add
    code: str
    amount: float | None = None
    shares: int | None = None


@dataclass
class EquityPoint:
    """每日净值点。"""

    date: str
    total_value: float
    cash: float
    settling: float
    positions_value: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BacktestResult:
    """回测任务结果汇总。"""

    task_id: str
    strategy: str
    params: dict
    start_date: str
    end_date: str
    metrics: dict
    trades: list[dict] = field(default_factory=list)
    equity: list[dict] = field(default_factory=list)
