"""选股内核调度测试：取数复用、股票池上限、异常暴露与策略参数注入。"""

import pandas as pd
import pytest


def rising_df(rows=32, breakout=True):
    """构造同时命中趋势回踩与箱体突破的价格序列（供内核测试复用）。"""
    closes = [9.5 + 0.5 * i / 30 for i in range(rows - 1)]
    if breakout:
        closes.append(10.30)
    return pd.DataFrame({
        "date": pd.date_range("2026-08-01", periods=len(closes)),
        "open": [c * 0.998 for c in closes],
        "high": [c * 1.005 for c in closes],
        "low": [c * 0.995 for c in closes],
        "close": closes,
        "volume": [1000.0] * len(closes),
    })


class FakeSource:
    """满足 BaseDataSource 契约的假数据源，记录取数次数。"""

    def __init__(self, stocks=None, frame=None, fail_codes=(), empty=False):
        self.stocks = stocks if stocks is not None else [
            {"code": "000001", "name": "平安银行"},
            {"code": "000002", "name": "万科A"},
        ]
        self.frame = frame if frame is not None else rising_df()
        self.fail_codes = set(fail_codes)
        self.empty = empty
        self.calls: list[str] = []
        self.closed = False

    def get_stock_list(self):
        return list(self.stocks)

    def get_kline(self, code):
        self.calls.append(code)
        if code in self.fail_codes:
            raise ConnectionError(f"模拟取数中断 {code}")
        return None if self.empty else self.frame

    def name_of(self, code):
        return {"000003": "名不经传"}.get(code, "")

    def max_date(self):
        return "2026-09-11"

    def close(self):
        self.closed = True


def config(stock_limit=0, tolerate=True):
    return {
        "datasource": {"type": "parquet"},
        "selector": {
            "default_strategies": ["trend_rebound", "box_oscillate"],
            "tolerate_errors": tolerate,
            "stock_limit": stock_limit,
        },
        "strategy_params": {
            "trend_rebound": {"period": 20, "pullback_pct": 0.05},
            "box_oscillate": {"period": 30, "breakout_pct": 0.02},
        },
    }


class TestRunSelect:
    def run(self, ds, **kw):
        from stock_selector.core.selector import run_select

        return run_select(datasource=ds, config=config(**kw))

    def test_both_strategies_hit_on_breakout_frame(self):
        results = self.run(FakeSource())
        assert {r.signal for r in results} == {"trend_rebound", "box_oscillate"}

    def test_each_stock_fetched_once_for_all_strategies(self):
        ds = FakeSource()
        self.run(ds)
        assert ds.calls == ["000001", "000002"]          # 2 只 × 2 策略 = 只取数 2 次

    def test_stock_limit_trims_universe(self):
        ds = FakeSource(stocks=[{"code": "000001", "name": "A"}, {"code": "000002", "name": "B"}])
        results = self.run(ds, stock_limit=1)
        assert ds.calls == ["000001"]
        assert results                                    # 命中不为空说明裁剪后仍可选股

    def test_names_filled_from_stock_list(self):
        results = self.run(FakeSource())
        assert {r.name for r in results} == {"平安银行", "万科A"}

    def test_name_falls_back_to_datasource(self):
        ds = FakeSource(stocks=[{"code": "000003", "name": ""}])
        results = self.run(ds)
        assert results and {r.name for r in results} == {"名不经传"}

    def test_code_filled_when_strategy_omits(self):
        results = self.run(FakeSource())
        assert {r.code for r in results} == {"000001", "000002"}

    def test_per_stock_failure_tolerated(self):
        ds = FakeSource(fail_codes={"000001"})
        results = self.run(ds)
        assert {r.code for r in results} == {"000002"}

    def test_per_stock_failure_raises_when_intolerant(self):
        ds = FakeSource(fail_codes={"000001"})
        with pytest.raises(ConnectionError):
            self.run(ds, tolerate=False)

    def test_unknown_strategy_rejected(self):
        from stock_selector.core.selector import run_select

        with pytest.raises(ValueError, match="未注册的策略"):
            run_select(strategies=["ghost"], datasource=FakeSource(), config=config())

    def test_empty_universe_raises_runtime_error(self):
        """全市场取不到任何行情必须报错，而不是静默返回 0 命中。"""
        from stock_selector.core.selector import run_select

        with pytest.raises(RuntimeError, match="未返回任何行情"):
            run_select(datasource=FakeSource(empty=True), config=config())

    def test_all_fetch_failures_raise_runtime_error(self):
        from stock_selector.core.selector import run_select

        ds = FakeSource(stocks=[{"code": "000001", "name": "A"}], fail_codes={"000001"})
        cfg = config()
        cfg["selector"]["tolerate_errors"] = True
        with pytest.raises(RuntimeError, match="未返回任何行情"):
            run_select(datasource=ds, config=cfg)

    def test_as_json_returns_serialisable_text(self):
        from stock_selector.core.selector import run_select

        out = run_select(datasource=FakeSource(), config=config(), as_json=True)
        assert isinstance(out, str) and out.lstrip().startswith("[")
        assert '"trend_rebound"' in out

    def test_injected_datasource_not_closed(self):
        ds = FakeSource()
        self.run(ds)
        assert ds.closed is False         # 所有权属于调用方

    def test_created_datasource_is_closed(self, monkeypatch):
        """内核自建的数据源必须释放，避免 HTTP 服务下连接泄漏。"""
        from stock_selector.core import selector as mod

        ds = FakeSource()
        monkeypatch.setattr(mod, "_create_datasource", lambda cfg: ds)
        mod.run_select(config=config())
        assert ds.closed is True


class TestStrategyLoader:
    def test_params_injected_by_strategy_name(self):
        from stock_selector.core.strategy_loader import StrategyLoader

        loader = StrategyLoader(config()["strategy_params"]).load_all()
        assert loader.create("trend_rebound").config == {"period": 20, "pullback_pct": 0.05}
        assert loader.create("box_oscillate").config == {"period": 30, "breakout_pct": 0.02}

    def test_create_is_case_insensitive(self):
        from stock_selector.core.strategy_loader import StrategyLoader

        loader = StrategyLoader({}).load_all()
        assert loader.create(" Trend_Rebound ").config == {}

    def test_unknown_name_lists_available(self):
        from stock_selector.core.strategy_loader import StrategyLoader

        loader = StrategyLoader({}).load_all()
        with pytest.raises(KeyError, match="trend_rebound"):
            loader.create("nope")

    def test_registered_names_are_snake_case(self):
        from stock_selector.core.strategy_loader import StrategyLoader

        names = StrategyLoader({}).load_all().names()
        assert "trend_rebound" in names and "box_oscillate" in names
