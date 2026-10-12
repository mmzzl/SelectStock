"""风控层：信号之后、下单之前的独立闸门。

策略只产生信号，信号不等于一定要下这个单——能不能成交（涨跌停、流动性）、买多少
（单票仓位上限）、大概什么价成交（滑点预估）、值不值得买（买入风险分）由这里判定。

用法（选股内核与盘中告警链路共用同一个引擎，口径不会分叉）：

    from stock_selector.risk import RiskEngine, build_context

    engine = RiskEngine.from_settings(config)          # 读 settings.yaml 的 risk: 子树
    decision = engine.review(build_context("600000", "buy", day="2026-10-12", kline=df))
    if not decision.blocked:
        ...                                            # decision.shares / est_fill_price / reason

模块划分：`snapshot` 取行情口径（涨跌停价/成交额），`context` 汇成一次判定所需的全部输入，
`sizing` 做额度与滑点的算术，`rules` 是五条规则，`engine` 按序执行并给出最终决策。
"""

from .context import RiskContext, build
from .engine import ADJUST, ALLOW, BLOCK, Decision, RiskConfig, RiskEngine
from .rules import Verdict
from .snapshot import get as get_snapshot

__all__ = ["RiskContext", "build", "build_context", "RiskConfig", "RiskEngine", "Decision",
           "Verdict", "ALLOW", "BLOCK", "ADJUST", "get_snapshot"]

build_context = build
