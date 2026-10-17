"""回测引擎：逐日推进、资金循环撮合、个股状态机。

撮合按 A 股的可执行性来（判据真源在 `executability.py`，与逐笔扫描同一套）：
涨停贴着线买不进、一字板当天没有对手盘卖不掉、停牌押着等下一个有行情的日子。
之前的版本只看收盘价就假定成交，Sharpe/回撤里混着一笔「撮合假设的钱」。
"""

from __future__ import annotations

import uuid

from stock_selector.backtest import executability
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
        progress_cb=None,
        params: dict | None = None,
        cash_policy=None,
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
        self.params = dict(params or {})
        self.progress_cb = progress_cb
        # 组合层现金调度（批次 E）：None = 完全不干预，与现行为逐条相同
        self.cash_policy = cash_policy
        self._initial_capital = float(capital)
        self.last_close: dict[str, float] = {}
        # 加仓指令只带代码，成交记录要名称，就在买入时把代码→名称记下来
        self._names: dict[str, str] = {}
        # 押着待卖的仓位：code → (股数或 None=清仓, 已经等了多少个交易日)
        self._want_sell: dict[str, tuple[int | None, int]] = {}
        self._exec = {"buy_blocked": 0, "add_blocked": 0, "sell_blocked": 0,
                      "sell_wait_days_max": 0, "pct_unknown": 0,
                      "sell_wait_by_code": {}}
        self._exec_reasons: dict[str, int] = {}

    # ---- 可执行性计数 ----
    def _count(self, kind: str, reason: str) -> None:
        key = {"buy": "buy_blocked", "add": "add_blocked", "sell": "sell_blocked"}[kind]
        self._exec[key] += 1
        tag = f"{kind}:{reason}"
        self._exec_reasons[tag] = self._exec_reasons.get(tag, 0) + 1

    def _entry_price(self, date: str, code: str, prev_close, kind: str) -> float | None:
        """能不能按当日收盘买/加这只票：可以就返回成交价，被拦就返回 None 并记账。

        买入和加仓是同一件事（都是付钱建仓），所以共用这段判定。
        """
        row = self.data.row(date, code)
        if row is None:
            self._count(kind, "suspended")                  # 停牌：没有行情行，这笔放弃
            return None
        pct = executability.day_change_pct(row, prev_close)
        if pct is None:
            self._exec["pct_unknown"] += 1        # 涨跌停判不了（数据缺 pct 又缺昨收）：放行但要记出来
        why = executability.buy_blocked(code, pct, row.get("high"), row.get("low"))
        if why:
            self._count(kind, why)
            return None
        px = float(row["close"])
        self.last_close[code] = px
        return px

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
        total_dates = len(dates)
        report_step = max(1, total_dates // 100)
        # 全市场逐日快照要读遍所有 parquet 并常驻内存，只用自带取数的策略（KernelStrategy）声明跳过
        needs_market_prices = getattr(self.strategy, "needs_market_prices", True)

        for idx, date in enumerate(dates, 1):
            if self.progress_cb and (idx % report_step == 0 or idx == total_dates):
                self.progress_cb(idx, total_dates, date)

            # 「昨收」必须在今天任何价格被记录之前取：涨跌停判定只能用当天之前的价
            prev_close = dict(self.last_close)
            broker.settle()

            holdings = list(broker.positions)
            prices = self._valuation_prices(date, holdings)
            if self.cash_policy is not None:
                # 当天能动用多少现金，由组合层调度决定；判据、出场、撮合一概不碰
                broker.cash_floor = self.cash_policy.floor(date, broker.total_present_value(prices))
            instructions = proxy.evaluate_holdings(date, holdings, prices)

            # ① 卖出：今天的指令先进队列，和之前押着待卖的一起按「当天卖不卖得掉」撮合
            for inst in instructions:
                if inst.side == "sell" and inst.code in broker.positions \
                        and inst.code not in self._want_sell:
                    self._want_sell[inst.code] = (inst.shares, 0)
            for code in sorted(self._want_sell):
                shares, waited = self._want_sell[code]
                if code not in broker.positions:            # 已经清掉了
                    del self._want_sell[code]
                    continue
                row = self.data.row(date, code)
                why = executability.sell_blocked(row.get("high"), row.get("low")) if row \
                    else "suspended"
                if why:
                    # 仓位还在手上，只能等：真实账户不会因为你卖不掉就把这笔从账上删掉，
                    # 所以引擎不像逐笔扫描那样到 REPAIR_MAX_DAYS 就丢单，只把押了多少天记出来
                    self._want_sell[code] = (shares, waited + 1)
                    self._exec["sell_wait_days_max"] = max(self._exec["sell_wait_days_max"], waited + 1)
                    self._count("sell", why)
                    continue
                px = float(row["close"])
                self.last_close[code] = px
                broker.sell(code, date, px, shares=shares)
                del self._want_sell[code]
                if waited:
                    # 押了多少天才出掉要留在账上：一次押上百个交易日多半是日线断档，不是真停牌
                    self._exec["sell_wait_by_code"][code] = waited

            for inst in instructions:
                if inst.side == "add" and inst.code in broker.positions and inst.amount:
                    px = self._entry_price(date, inst.code, prev_close.get(inst.code), "add")
                    if px is None:
                        continue
                    try:
                        broker.add(inst.code, self._names.get(inst.code) or inst.code,
                                   date, px, inst.amount)
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
                    px = self._entry_price(date, o.code, prev_close.get(o.code), "buy")
                    if px is None:
                        continue
                    self._names[o.code] = o.name
                    # 买单被拒是静默的（只数进 cap_blocked/资金不足计数），历史上就静默掉过一整年：
                    # 单笔金额固定 20,000 而单票上限是「总资产×20%」，账户缩水后每笔都超限，
                    # 基准于是 2021 全年 0 笔、净值死住，任何多年窗的回测都会踩这个坑
                    # （线上回测页面走的是同一份代码， sizing 已在 KernelStrategy 修好；
                    # 规则模式仍按用户给的固定金额下单，超限会记进 cap_blocked，读数时必须看这一列）。
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
            params=self.params,
            start_date=dates[0] if dates else "",
            end_date=dates[-1] if dates else "",
            metrics=metrics.to_dict(),
            trades=[self._trade_dict(t) for t in broker.trades],
            equity=[p.to_dict() for p in equity_points],
            executability={**self._exec, "reasons": dict(self._exec_reasons),
                           "cash_floor_blocked": self.broker.floor_blocked,
                           "cap_blocked": self.broker.cap_blocked,
                           "sell_pending": {c: w for c, (_, w) in sorted(self._want_sell.items())}},
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
