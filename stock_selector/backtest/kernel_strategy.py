"""把选股插件策略接到回测引擎上：入场与实盘选股共用同一段 as-of 判据。

选股侧 `BaseSelectStrategy.signal_at(df)` 只依据传入切片（截至基准日）判断是否出信号，
本适配器按交易日逐日切片调用它，于是"策略在历史上某天会不会买"与"策略今天选出什么"
是同一份代码的两次运行，可互相验证。

出场不在选股策略的职责内（策略只回答"此刻是否该买"），因此由本适配器按
持有天数 / 止损 / 止盈三条机械规则决定，回测结论中的收益依赖这组参数而非策略本身。
"""

from __future__ import annotations

from stock_selector.backtest.models import Instruction, Order


class KernelStrategy:
    """入场判据 = 选股插件的 signal_at，出场 = 机械规则；不消费引擎的全市场报价快照。

    取数一律走 `ParquetProvider.window()`，因此声明 `needs_market_prices = False`，
    让引擎跳过"逐日读遍全部 parquet"这一步（全市场回测的分钟级开销大头就在这里）。
    """

    needs_market_prices = False

    def __init__(
        self,
        strategy,
        provider,
        target_amount: float = 20000.0,
        universe: list[str] | None = None,
        max_hold_days: int = 10,
        stop_loss_pct: float = 0.05,
        take_profit_pct: float = 0.0,
        reject_gate=None,
    ) -> None:
        self.strategy = strategy
        self.provider = provider
        self.target_amount = float(target_amount)
        self.universe = list(universe) if universe else None
        self.max_hold_days = int(max_hold_days or 0)
        self.stop_loss_pct = float(stop_loss_pct or 0.0)
        self.take_profit_pct = float(take_profit_pct or 0.0)
        self.reject_gate = reject_gate
        self.broker = None
        self.rejected = 0
        self._entry: dict[str, str] = {}
        self._date_index: dict[str, int] | None = None

    def attach_broker(self, broker) -> None:
        """由 BacktestEngine 在开跑时注入，用于读取持仓成本与成交历史。"""
        self.broker = broker

    # ---- 入场：与实盘选股同源 ----
    def signal_on(self, date: str, code: str):
        """基准日 as-of 信号。停牌或基准日无行时返回 None，不用历史行冒充当日决策。"""
        df = self.provider.window(code, date, int(self.strategy.required_rows() or 0))
        if df is None or len(df) < 2 or str(df["date"].iloc[-1]) != date:
            return None
        hit = self.strategy.signal_at(df)
        if hit is not None and not hit.name and "name" in df.columns:
            hit.name = str(df["name"].iloc[-1])
        return hit

    def _codes(self) -> list[str]:
        return self.universe if self.universe is not None else self.provider.codes()

    def _order_amount(self) -> float:
        """单笔金额随当前总资产收缩：`min(目标金额, 总资产 × 单票上限比例)`。

        固定金额是个仪器缺陷（批次 E 的基准就是这么废掉的）：`_check_single_stock_cap` 的上限是
        `总资产 × position_cap_pct`，账户从 100k 缩水到 49k 后上限只剩 9,886 < 固定 20,000，
        于是**每一笔买单都被超限拒掉**，而引擎 `except ValueError` 静默跳过——基准 2021 全年 0 笔、
        净值死住，整条对照基准变成「一个停盘的策略」。这里用与那条校验同一个
        `total_present_value({})` 口径，所以新票建仓一定落在上限内，长窗回测不会后半段冻死。
        """
        if self.broker is None:
            return self.target_amount
        cap_value = self.broker.total_present_value({}) * self.broker.position_cap_pct
        return min(self.target_amount, cap_value)

    def propose_buys(self, date: str, cash: float, prices: dict) -> list[Order]:
        """当日全市场候选买入，按信号强度降序排（引擎按仓位上限从头成交，排序即选股优先级）。"""
        held = set(self.broker.positions) if self.broker is not None else set()
        scored: list[tuple[float, str, object]] = []
        for code in self._codes():
            if code in held:
                continue
            hit = self.signal_on(date, code)
            if hit is None:
                continue
            # 闸门挂在命中之后、下单之前，与实盘 `selector` 的 risk_filter 同一个位置：
            # 信号照出，能不能成为决策由闸门判。判不到（历史没打分）按放行处理。
            if self.reject_gate is not None and not self.reject_gate(date, code):
                self.rejected += 1
                continue
            scored.append((hit.score if hit.score is not None else 0.0, code, hit))
        scored.sort(key=lambda item: (-item[0], item[1]))
        amount = self._order_amount()
        return [
            Order(code=code, amount=amount, name=hit.name or "")
            for _, code, hit in scored
        ]

    # ---- 出场：机械规则，与选股判据无关 ----
    def evaluate_holdings(self, date: str, holdings: list[str], prices: dict) -> list[Instruction]:
        if self.broker is None:
            return []
        for code in list(self._entry):
            if code not in self.broker.positions:
                del self._entry[code]

        instructions: list[Instruction] = []
        for code in holdings:
            position = self.broker.positions.get(code)
            price = prices.get(code)
            if position is None or price is None:
                continue
            entry = self._entry.get(code) or self._find_entry(code)
            if entry:
                self._entry[code] = entry
            cost = position.avg_price
            if self.stop_loss_pct and price <= cost * (1 - self.stop_loss_pct):
                instructions.append(Instruction(side="sell", code=code))
            elif self.take_profit_pct and price >= cost * (1 + self.take_profit_pct):
                instructions.append(Instruction(side="sell", code=code))
            elif self.max_hold_days and self._held_days(entry or date, date) >= self.max_hold_days:
                instructions.append(Instruction(side="sell", code=code))
        return instructions

    def _find_entry(self, code: str) -> str:
        for trade in reversed(self.broker.trades):
            if trade.code == code and trade.side in ("buy", "add"):
                return trade.date
        return ""

    def _held_days(self, entry: str, date: str) -> int:
        if self._date_index is None:
            self._date_index = {d: i for i, d in enumerate(self.provider.dates())}
        return self._date_index.get(date, 0) - self._date_index.get(entry, 0)
