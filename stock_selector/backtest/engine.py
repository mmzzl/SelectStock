"""回测引擎：逐日推进、资金循环撮合、个股状态机。"""

from __future__ import annotations

import uuid

from stock_selector.backtest.broker import Broker
from stock_selector.backtest.metrics import Metrics
from stock_selector.backtest.models import BacktestResult, EquityPoint


class BacktestEngine:
    """按个股状态机逐日撮合：卖出优先 -> 加仓 -> 买入新标的 -> 记录净值。"""

    def __init__(
        self,
        strategy,
        data,
        capital: float = 100000.0,
        max_positions: int = 5,
        position_cap_pct: float = 0.2,
        commission_rate: float = 0.00025,
        commission_min: float = 5.0,
        stamp_tax_rate: float = 0.0005,
        task_id: str | None = None,
        strategy_name: str = "",
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> None:
        self.strategy = strategy
        self.data = data
        self.start_date = start_date
        self.end_date = end_date
        self.broker = Broker(
            capital=capital,
            max_positions=max_positions,
            position_cap_pct=position_cap_pct,
            commission_rate=commission_rate,
            commission_min=commission_min,
            stamp_tax_rate=stamp_tax_rate,
        )
        self.task_id = task_id or str(uuid.uuid4())
        self.strategy_name = strategy_name or type(strategy).__name__
        self._initial_capital = float(capital)
        self.last_close: dict[str, float] = {}

    def _known_price(self, date: str, code: str) -> float | None:
        px = self.data.price(date, code)
        if px is not None:
            self.last_close[code] = px
            return px
        return self.last_close.get(code)

    def _valuation_prices(self, date: str, codes: list[str]) -> dict[str, float]:
        pmap = {}
        for code in codes:
            px = self._known_price(date, code)
            if px is not None:
                pmap[code] = px
        return pmap

    def run(self) -> BacktestResult:
        broker = self.broker
        proxy = _StrategyProxy(self.strategy, self.data, self._known_price)
        # 需要持仓成本/成交历史的策略（如复用选股插件策略的 KernelStrategy）自行认领账户
        attach = getattr(self.strategy, "attach_broker", None)
        if attach is not None:
            attach(broker)

        equity_points: list[EquityPoint] = []
        dates = self._within_window(self.data.dates())
        # 全市场逐日快照要读遍所有 parquet 并常驻内存，只用自带取数的策略（KernelStrategy）声明跳过
        needs_market_prices = getattr(self.strategy, "needs_market_prices", True)

        for date in dates:
            broker.settle()

            holdings = list(broker.positions)
            prices = self._valuation_prices(date, holdings)
            instructions = proxy.evaluate_holdings(date, holdings, prices)

            for inst in instructions:
                if inst.side == "sell" and inst.code in broker.positions:
                    px = self.data.price(date, inst.code)
                    if px is not None:
                        self.last_close[inst.code] = px
                        broker.sell(inst.code, date, px, shares=inst.shares)

            for inst in instructions:
                if inst.side == "add" and inst.code in broker.positions:
                    amount = inst.amount
                    if amount:
                        px = self.data.price(date, inst.code)
                        if px is not None:
                            self.last_close[inst.code] = px
                            try:
                                broker.add(inst.code, inst.name, date, px, amount)
                            except ValueError:
                                pass

            if len(broker.positions) < broker.max_positions:
                all_prices = self.data.prices(date) if needs_market_prices else {}
                buys = proxy.propose_buys(date, broker.cash, all_prices)
                for o in buys:
                    if len(broker.positions) >= broker.max_positions:
                        break
                    if o.code in broker.positions:
                        continue
                    px = self.data.price(date, o.code)
                    if px is None:
                        continue
                    self.last_close[o.code] = px
                    try:
                        broker.buy(o.code, o.name, date, px, o.amount)
                    except ValueError:
                        continue

            holdings = list(broker.positions)
            prices = self._valuation_prices(date, holdings)
            pv = broker.positions_value(prices)
            equity_points.append(EquityPoint(
                date=date,
                total_value=broker.cash + broker.settling + pv,
                cash=broker.cash,
                settling=broker.settling,
                positions_value=pv,
            ))

        self.broker = broker

        closed_pnls = [t.pnl for t in broker.trades if t.side == "sell" and t.pnl is not None]
        metrics = Metrics(
            equity_curve=[p.total_value for p in equity_points],
            initial_capital=self.broker_initial(),
            closed_pnls=closed_pnls,
        )

        return BacktestResult(
            task_id=self.task_id,
            strategy=self.strategy_name,
            params={},
            start_date=dates[0] if dates else "",
            end_date=dates[-1] if dates else "",
            metrics=metrics.to_dict(),
            trades=[self._trade_dict(t) for t in broker.trades],
            equity=[p.to_dict() for p in equity_points],
        )

    def _cache_prices(self, date: str, codes: list[str]) -> None:
        for code in codes:
            self._known_price(date, code)

    def _within_window(self, dates: list[str]) -> list[str]:
        filtered = dates
        if self.start_date:
            filtered = [d for d in filtered if d >= self.start_date]
        if self.end_date:
            filtered = [d for d in filtered if d <= self.end_date]
        return filtered

    def broker_initial(self) -> float:
        return self._initial_capital

    def _trade_dict(self, t) -> dict:
        return {
            "task_id": self.task_id,
            "code": t.code,
            "name": t.name,
            "side": t.side,
            "date": t.date,
            "price": t.price,
            "shares": t.shares,
            "amount": t.amount,
            "fee": t.fee,
            "pnl": t.pnl,
        }


class _StrategyProxy:
    """对策略的两个回调接口做统一适配，屏蔽 data 细节。"""

    def __init__(self, strategy, data, known_price):
        self.strategy = strategy
        self.data = data
        self.known_price = known_price

    def evaluate_holdings(self, date, holdings, prices):
        fn = getattr(self.strategy, "evaluate_holdings", None)
        if fn is None:
            return []
        return fn(date, holdings, prices) or []

    def propose_buys(self, date, cash, prices):
        fn = getattr(self.strategy, "propose_buys", None)
        if fn is None:
            return []
        return fn(date, cash, prices) or []
