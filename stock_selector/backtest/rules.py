"""实盘规则策略适配器：读取 trading_rules，按个股状态机产出买卖指令。"""

from __future__ import annotations

from stock_selector.backtest.models import Instruction, Order
from stock_selector.backtest.rule_eval import RuleEvaluator


class RulesStrategy:
    """把 trading_rules 的 buy/sell/add 条件翻译为引擎可消费的指令/订单。

    与引擎解耦：propose_buys 负责"当日候选"，evaluate_holdings 负责持仓迁移。
    """

    def __init__(
        self,
        provider,
        buy_rule: str,
        sell_rule: str,
        add_rule: str | None = None,
        target_amount: float = 20000.0,
    ) -> None:
        self.provider = provider
        self.buy_rule = buy_rule
        self.sell_rule = sell_rule
        self.add_rule = add_rule
        self.target_amount = target_amount
        self._eval = RuleEvaluator()

    def propose_buys(self, date: str, cash: float, prices: dict) -> list[Order]:
        """当日全市场候选买入（按代码排序），新买入目标金额固定。"""
        orders: list[Order] = []
        if not self.buy_rule:
            return orders
        rows = self.provider.rows(date)
        for code, row in sorted(rows.items()):
            if self._eval.eval(self.buy_rule, row):
                orders.append(Order(code=code, amount=self.target_amount,
                                    name=str(row.get("name", ""))))
        return orders

    def evaluate_holdings(self, date: str, holdings: list[str], prices: dict) -> list[Instruction]:
        """对持仓返回 sell/add 迁移指令。"""
        instr: list[Instruction] = []
        rows = self.provider.rows(date, codes=holdings)
        for code in holdings:
            row = rows.get(code)
            if row is None:
                continue
            if self.sell_rule and self._eval.eval(self.sell_rule, row):
                instr.append(Instruction(side="sell", code=code))
            elif self.add_rule and self._eval.eval(self.add_rule, row):
                instr.append(Instruction(side="add", code=code,
                                         amount=self.target_amount))
        return instr
