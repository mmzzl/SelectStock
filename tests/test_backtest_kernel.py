"""选股策略与回测同源（KernelStrategy）测试，重点是"回测不得看未来"。"""

import pandas as pd
import pytest

from stock_selector.backtest.engine import BacktestEngine
from stock_selector.backtest.kernel_strategy import KernelStrategy
from stock_selector.backtest.provider import ParquetProvider
from stock_selector.core.base_strategy import BaseSelectStrategy
from stock_selector.models.result import SelectResult


def dates(n, start="2026-01-05"):
    return pd.date_range(start, periods=n).strftime("%Y-%m-%d").tolist()


def write_stock(dirpath, code, closes, name="测试股", volume=1000):
    closes = [float(c) for c in closes]
    df = pd.DataFrame({
        "date": dates(len(closes)),
        "open": closes,
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
        "close": closes,
        "volume": [float(volume)] * len(closes),
        # 撮合要按 pct 判涨跌停，真实日线里有这列，夹具就得有
        "pct": [0.0] + [(b / a - 1.0) * 100.0 for a, b in zip(closes, closes[1:])],
    })
    df["code"] = code
    df["name"] = name
    df.to_parquet(dirpath / f"{code}.parquet", index=False, compression="zstd")
    return df


class BandStrategy(BaseSelectStrategy):
    """可控的测试策略：收盘价落在 (10, 20) 区间即出信号，分数=收盘价/100。"""

    def required_rows(self) -> int:
        return 5

    def signal_at(self, df):
        close = float(df["close"].iloc[-1])
        if not 10 < close < 20:
            return None
        return SelectResult(code="", name="", signal="band", reason=f"close={close}",
                            score=round(close / 100, 4))


@pytest.fixture
def provider(tmp_path):
    write_stock(tmp_path, "000001", [8, 9, 11, 12, 13, 14, 15, 16, 17, 18])
    write_stock(tmp_path, "000002", [8, 9, 10.5, 11.5, 12.5, 13.5, 14.5, 15.5, 16.5, 17.5])
    return ParquetProvider(tmp_path)


class TestProviderWindow:
    def test_window_never_includes_future_rows(self, provider):
        df = provider.window("000001", "2026-01-08", rows=100)
        assert list(df["date"]) == ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]

    def test_window_caps_at_requested_rows(self, provider):
        df = provider.window("000001", "2026-01-14", rows=3)
        assert len(df) == 3 and df["date"].iloc[-1] == "2026-01-14"

    def test_window_empty_before_history(self, provider):
        assert provider.window("000001", "2026-01-04", rows=5) is None

    def test_window_of_missing_code_is_none(self, provider):
        assert provider.window("999999", "2026-01-08", rows=5) is None

    def test_codes_lists_universe(self, provider):
        assert provider.codes() == ["000001", "000002"]


class TestKernelSignal:
    def strategy(self, provider, **kw):
        return KernelStrategy(BandStrategy(), provider, **kw)

    def test_signal_fires_only_on_base_date_bar(self, provider):
        k = self.strategy(provider)
        assert k.signal_on("2026-01-07", "000001") is not None      # close=11
        assert k.signal_on("2026-01-05", "000001") is None          # close=8 未进区间

    def test_suspended_base_date_produces_no_stale_signal(self, provider, tmp_path):
        """基准日停牌时不能用更早的历史行冒充当日决策，否则回测会买到停牌的票。"""
        write_stock(tmp_path, "600000", [8, 9, 11, 11, 11])          # 只到 01-08
        k = self.strategy(ParquetProvider(tmp_path))
        assert k.signal_on("2026-01-08", "600000") is not None
        assert k.signal_on("2026-01-12", "600000") is None

    def test_signal_ignores_any_future_data(self, provider, tmp_path):
        """追加暴跌的未来行之后，历史基准日的 as-of 判据必须一字不差地复现。"""
        before = self.strategy(provider).signal_on("2026-01-14", "000001")
        write_stock(tmp_path, "000001", [8, 9, 11, 12, 13, 14, 15, 16, 17, 18, 3, 2, 1])
        after = self.strategy(ParquetProvider(tmp_path)).signal_on("2026-01-14", "000001")
        assert before is not None and after is not None
        assert before.reason == after.reason and before.score == after.score

    def test_name_resolved_from_frame(self, provider):
        hit = self.strategy(provider).signal_on("2026-01-07", "000001")
        assert hit.name == "测试股"

    def test_orders_sorted_by_score_desc(self, tmp_path):
        """分数高的候选排在前面，引擎按仓位上限从头成交，排序即选股优先级。"""
        write_stock(tmp_path, "000001", [12] * 6)     # close 12 -> score .12
        write_stock(tmp_path, "000002", [18] * 6)     # close 18 -> score .18
        k = KernelStrategy(BandStrategy(), ParquetProvider(tmp_path))
        orders = k.propose_buys("2026-01-10", 100000, {})
        assert [o.code for o in orders] == ["000002", "000001"]
        assert all(o.amount == k.target_amount for o in orders)

    def test_already_held_not_re_proposed(self, tmp_path, provider):
        k = self.strategy(provider, target_amount=1000)

        class _Pos:
            positions = {"000001": None}
            trades = []

        k.attach_broker(_Pos())
        assert [o.code for o in k.propose_buys("2026-01-10", 100000, {})] == ["000002"]


class TestKernelExits:
    class _Broker:
        def __init__(self, positions, trades):
            self.positions = positions
            self.trades = trades

    def _position(self, shares=1000, avg=10.0):
        from stock_selector.backtest.broker import Position

        return Position(shares=shares, avg_price=avg, cost=shares * avg)

    def test_stop_loss_triggers_sell(self, provider):
        k = KernelStrategy(BandStrategy(), provider, stop_loss_pct=0.1, max_hold_days=0)
        k.attach_broker(self._Broker({"000001": self._position(avg=15.0)}, []))
        instructions = k.evaluate_holdings("2026-01-14", ["000001"], {"000001": 13.0})
        assert [i.side for i in instructions] == ["sell"]

    def test_take_profit_triggers_sell(self, provider):
        k = KernelStrategy(BandStrategy(), provider, stop_loss_pct=0, take_profit_pct=0.1,
                           max_hold_days=0)
        k.attach_broker(self._Broker({"000001": self._position(avg=12.0)}, []))
        instructions = k.evaluate_holdings("2026-01-14", ["000001"], {"000001": 14.0})
        assert [i.side for i in instructions] == ["sell"]

    def test_max_hold_days_forces_exit(self, provider):
        from stock_selector.backtest.broker import Trade

        k = KernelStrategy(BandStrategy(), provider, stop_loss_pct=0, max_hold_days=2)
        broker = self._Broker(
            {"000001": self._position(avg=11.0)},
            [Trade(code="000001", side="buy", date="2026-01-08", price=11.0, shares=1000)],
        )
        k.attach_broker(broker)
        assert k.evaluate_holdings("2026-01-09", ["000001"], {"000001": 12.0}) == []
        sold = k.evaluate_holdings("2026-01-10", ["000001"], {"000001": 12.0})
        assert [i.side for i in sold] == ["sell"]

    def test_holding_kept_when_no_rule_hit(self, provider):
        k = KernelStrategy(BandStrategy(), provider, stop_loss_pct=0.1, max_hold_days=99)
        k.attach_broker(self._Broker({"000001": self._position(avg=11.0)}, []))
        assert k.evaluate_holdings("2026-01-14", ["000001"], {"000001": 13.0}) == []

    def test_exit_entry_date_dropped_after_position_closed(self, provider):
        from stock_selector.backtest.broker import Trade

        k = KernelStrategy(BandStrategy(), provider, stop_loss_pct=0, max_hold_days=1)
        broker = self._Broker({}, [Trade(code="000001", side="buy", date="2026-01-08")])
        k.attach_broker(broker)
        k._entry["000001"] = "2026-01-08"
        k.evaluate_holdings("2026-01-12", [], {})
        assert "000001" not in k._entry


class TestEngineIntegration:
    def test_engine_runs_kernel_strategy_and_respects_date_window(self, provider):
        k = KernelStrategy(BandStrategy(), provider, target_amount=20000,
                           stop_loss_pct=0.5, max_hold_days=3)
        engine = BacktestEngine(strategy=k, data=provider, capital=100000, max_positions=2,
                                start_date="2026-01-07", end_date="2026-01-12")
        result = engine.run()
        assert [p["date"] for p in result.equity] == dates(10)[2:8]
        buys = [t for t in result.trades if t["side"] == "buy"]
        assert buys and all(t["price"] >= 10 for t in buys)
        assert k.broker is engine.broker           # attach_broker 钩子生效

    def test_kernel_strategy_skips_market_price_snapshot(self, provider):
        """自带取数的策略不得触发全市场逐日快照 —— 那是全股票回测的分钟级开销来源。"""
        class SpyProvider(ParquetProvider):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.snapshot_calls = 0

            def prices(self, date, codes=None):
                self.snapshot_calls += 1
                return super().prices(date, codes)

        spy = SpyProvider(provider.data_dir)
        k = KernelStrategy(BandStrategy(), spy, stop_loss_pct=0.5, max_hold_days=3)
        result = BacktestEngine(strategy=k, data=spy, max_positions=2,
                                start_date="2026-01-07", end_date="2026-01-12").run()
        assert spy.snapshot_calls == 0
        assert [t for t in result.trades if t["side"] == "buy"]   # 不靠快照也能成交

    def test_price_consuming_strategy_still_gets_snapshot(self, provider):
        """缺省（规则策略）不改行为：仍然拿到全市场报价快照。"""
        seen: list[dict] = []

        class PricedStrategy:
            def propose_buys(self, date, cash, prices):
                seen.append(prices)
                return []

        BacktestEngine(strategy=PricedStrategy(), data=provider, max_positions=2,
                       start_date="2026-01-07", end_date="2026-01-08").run()
        assert seen and set(seen[0]) == {"000001", "000002"}

    def test_universe_limits_candidate_pool(self, provider):
        k = KernelStrategy(BandStrategy(), provider, universe=["000001"])
        assert {o.code for o in k.propose_buys("2026-01-10", 1e9, {})} == {"000001"}

    def test_reject_gate_blocks_only_the_code_it_rejects(self, provider):
        """闸门挂在买入腿：信号照出、下单前被拦，与实盘「先有信号再过风控」同一顺序。"""
        asked: list[tuple[str, str]] = []

        def gate(date, code):
            asked.append((date, code))
            return code != "000001"

        k = KernelStrategy(BandStrategy(), provider, reject_gate=gate)
        orders = k.propose_buys("2026-01-10", 1e9, {})
        assert {o.code for o in orders} == {"000002"}
        assert k.rejected == 1 and ("2026-01-10", "000001") in asked

    def test_reject_gate_not_asked_without_a_signal(self, provider):
        """没命中的票不该问闸门——它没产生信号，本来就不在决策入口上。"""
        asked: list[str] = []
        k = KernelStrategy(BandStrategy(), provider,
                           reject_gate=lambda d, c: asked.append(c) or True)
        k.propose_buys("2026-01-05", 1e9, {})
        assert asked == []

    def test_real_selection_strategy_is_usable_in_backtest(self, tmp_path):
        """真实插件策略（趋势回踩）不改一行代码即可被回测逐日求值，判据与实盘一致。"""
        from stock_selector.core.selector import create_strategy

        closes = [round(8 + 0.04 * i, 4) for i in range(40)]  # 缓步上行，收盘贴近 MA20
        write_stock(tmp_path, "000001", closes)
        provider = ParquetProvider(tmp_path)
        strategy = create_strategy("trend_rebound")
        k = KernelStrategy(strategy, provider)
        base = dates(len(closes))[-1]

        hit = k.signal_on(base, "000001")
        frame = provider.window("000001", base, strategy.required_rows())
        live = strategy.run(frame)[0]
        assert hit is not None and hit.signal == "trend_rebound"
        assert hit.reason == live.reason and hit.score == live.score
        assert hit.name == "测试股"
        assert k.signal_on(dates(len(closes))[-2], "000001") is not None   # 历史日可复现
