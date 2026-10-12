"""选股内核调度测试：取数复用、股票池上限、异常暴露与策略参数注入。"""

import pandas as pd
import pytest
from pathlib import Path

from stock_selector.datasource.base_source import BaseDataSource


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
        "volume": [20000.0] * len(closes),
    })


class FakeSource(BaseDataSource):
    """满足 BaseDataSource 契约的假数据源，记录取数次数与内核下发的取数需求。"""

    def __init__(self, stocks=None, frame=None, fail_codes=(), empty=False):
        super().__init__()
        self.stocks = stocks if stocks is not None else [
            {"code": "000001", "name": "平安银行"},
            {"code": "000002", "name": "万科A"},
        ]
        self.frame = frame if frame is not None else rising_df()
        self.fail_codes = set(fail_codes)
        self.empty = empty
        self.calls: list[str] = []
        self.closed = False
        self.prepared: dict = {}

    def prepare(self, columns=None, rows=0):
        self.prepared = {"columns": list(columns or []), "rows": rows}

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

    def test_datasource_told_what_strategies_need(self):
        """内核把各策略的数据需求并集下发给数据源，避免剪列后策略静默 0 命中。"""
        ds = FakeSource()
        self.run(ds)
        assert ds.prepared["rows"] == 40            # trend_rebound 30 / box_oscillate 40，取最大
        assert {"date", "open", "high", "low", "close", "volume"} <= set(ds.prepared["columns"])

    def test_result_metadata_backfilled(self):
        results = self.run(FakeSource())
        assert results
        for r in results:
            assert r.asof == "2026-09-11"           # 基准日来自数据源
            assert r.price == pytest.approx(10.30)  # 基准日收盘价
            assert r.strategy in {"trend_rebound", "box_oscillate"}

    def test_results_sorted_by_score_with_rank(self):
        results = self.run(FakeSource())
        scores = [r.score for r in results]
        assert all(s is not None for s in scores)
        assert scores == sorted(scores, reverse=True)
        assert [r.rank for r in results] == list(range(1, len(results) + 1))


class TestRanking:
    def test_unscored_results_sort_last_but_still_get_a_position(self):
        from stock_selector.core.selector import _rank
        from stock_selector.models import SelectResult

        items = [
            SelectResult(code="000002", name="", signal="s", reason=""),
            SelectResult(code="000001", name="", signal="s", reason="", score=0.5),
        ]
        ranked = _rank(items)
        assert [(r.code, r.rank) for r in ranked] == [("000001", 1), ("000002", 2)]

    def test_no_scores_keeps_production_order(self):
        from stock_selector.core.selector import _rank
        from stock_selector.models import SelectResult

        items = [SelectResult(code=c, name="", signal="s", reason="") for c in ("000002", "000001")]
        assert [r.code for r in _rank(items)] == ["000002", "000001"]


class TestUniverseExclusion:
    """按名字剔除非选股标的（货币/债券 ETF），见 settings.yaml 的实测依据。"""

    def run(self, ds, keywords):
        from stock_selector.core.selector import run_select

        cfg = config()
        cfg["selector"]["exclude_name_keywords"] = keywords
        return run_select(datasource=ds, config=cfg)

    def test_helper_matches_substring_and_ignores_blanks(self):
        from stock_selector.core.selector import _is_excluded

        assert _is_excluded("银华日利货币ETF", ["货币"]) is True
        assert _is_excluded("平安银行", ["货币"]) is False
        assert _is_excluded("", ["货币"]) is False
        assert _is_excluded("国债ETF", []) is False
        assert _is_excluded("国债ETF", ["", "债"]) is True

    def test_empty_name_is_never_excluded(self):
        """股票列表没名字、数据源也报不出名字时放行：宁可不剔也不误剔一只股票。"""
        ds = FakeSource(stocks=[{"code": "000001", "name": ""}])
        results = self.run(ds, ["货币"])
        assert {r.code for r in results} == {"000001"}

    def test_money_and_bond_etf_dropped_equity_kept(self):
        ds = FakeSource(stocks=[
            {"code": "000001", "name": "平安银行"},
            {"code": "511880", "name": "银华货币ETF"},
            {"code": "511010", "name": "国债ETF"},
            {"code": "511690", "name": "招商快线ETF"},
        ])
        results = self.run(ds, ["货币", "债", "快线"])
        assert {r.code for r in results} == {"000001"}

    def test_settings_keywords_do_not_hit_cashflow_equity_etf(self):
        """防回归：现金流ETF 是股票型基金，配置里出现"现金"就会把它错杀。"""
        from stock_selector.core.selector import _is_excluded, _load_config

        keywords = list(_load_config()["selector"]["exclude_name_keywords"])
        assert _is_excluded("景顺长城国证自由现金流ETF", keywords) is False
        assert _is_excluded("银华日利", keywords) is True

    def test_st_keyword_hits_only_warning_st_names(self):
        """"ST" 这个关键词的全部依据：全市场名字里含大写 ST 的必须恰好是风险警示股。

        真数据校验，快照缺失就跳过（无假数据可编，编出来的名字证明不了零误伤）。
        """
        import glob
        import re

        files = sorted(glob.glob("data/market_snapshot/*.parquet"))
        if not files:
            pytest.skip("no market snapshot in this checkout")
        import pandas as pd

        names = pd.read_parquet(files[-1])["name"].astype(str)
        hits = set(names[names.str.contains("ST", regex=False)])
        prefixed = set(names[names.str.match(r"^\*?ST")])
        assert hits == prefixed, f"误伤或漏剔：{sorted(hits ^ prefixed)[:5]}"
        assert len(hits) > 100

    def test_production_keywords_drop_st_names(self):
        """配置里的 "ST" 要真在链路上生效：风险警示股既不进结果，也不进风控判分。"""
        from stock_selector.core.selector import _load_config

        ds = FakeSource(stocks=[{"code": "000001", "name": "平安银行"},
                                {"code": "000016", "name": "*ST康佳A"}])
        keywords = list(_load_config()["selector"]["exclude_name_keywords"])
        assert {r.code for r in self.run(ds, keywords)} == {"000001"}


class TestStrategyContract:
    def test_run_is_the_list_form_of_signal_at(self):
        from stock_selector.core.selector import create_strategy

        strategy = create_strategy("trend_rebound")
        frame = rising_df()
        assert strategy.run(frame) == [strategy.signal_at(frame)]

    def test_unimplemented_strategy_warns_instead_of_silently_passing(self):
        from stock_selector.core.base_strategy import BaseSelectStrategy

        class Empty(BaseSelectStrategy):
            pass

        with pytest.warns(RuntimeWarning, match="不会产出"):
            assert Empty().run(rising_df()) == []

    def test_describe_reports_asof_and_data_needs(self):
        from stock_selector.core.selector import strategy_details

        by_name = {d["name"]: d for d in strategy_details()}
        assert by_name["trend_rebound"]["as_of"] is True
        assert by_name["trend_rebound"]["required_rows"] == 30
        assert by_name["box_oscillate"]["required_rows"] == 40
        assert by_name["trend_rebound"]["description"]


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


class TestRiskGateInKernel:
    """策略只产生信号，信号必须先过风控才成为决策（内核侧接线，规则本身见 test_risk_module）。"""

    def risk_cfg(self, **over):
        cfg = config()
        cfg["risk"] = {"enabled": True, "capital": 200000, "max_position_pct": 0.2,
                       "min_avg_amount": 0, "max_slippage_bp": 1e9, "max_drift_bp": 1e9,
                       "ml": {"enabled": False}}
        cfg["risk"].update(over)
        return cfg

    def run(self, ds, **over):
        from stock_selector.core.selector import run_select

        return run_select(datasource=ds, config=self.risk_cfg(**over))

    def test_allowed_signals_carry_final_decision(self):
        results = self.run(FakeSource())
        assert results
        for r in results:
            assert r.decision in ("allow", "adjust")
            assert r.extra["risk_verdict"] == r.decision
            assert r.extra["risk_action"] == "buy"

    def test_position_cap_becomes_suggested_shares(self):
        """额度 = 20 万 × 20% = 4 万，基准价 10.30 → 整手 3800 股。"""
        results = self.run(FakeSource())
        price = results[0].price
        assert price == pytest.approx(10.3)
        assert results[0].extra["risk_shares"] == int(40000 // (price * 100)) * 100 == 3800
        assert results[0].extra["risk_fill_price"] > price      # 买单按预估滑点后的价

    def test_limit_up_hit_is_dropped_not_output(self):
        """涨停买不进：命中的票整只剔除，不会带着一个下不了单的信号出现在结果里。"""
        frame = rising_df()
        frame["lu"] = [False] * (len(frame) - 1) + [True]
        assert self.run(FakeSource(frame=frame)) == []

    def test_thin_liquidity_is_dropped(self):
        # 假数据 volume=2 万手 ≈ 日均成交额 2000 万元：一只正常小盘票的量级
        assert self.run(FakeSource(), min_avg_amount=5e7) == []
        assert self.run(FakeSource(), min_avg_amount=5e6)       # 同数据在低下限下放行

    def test_order_too_big_for_turnover_is_dropped(self):
        """流动性下限只管「这票够不够大」，单子占比过高是另一道闸：1000 手 ≈ 103 万元日额，
        建议单 4 万元占用 4% > 2%，票本身合格也吃不到。"""
        frame = rising_df()
        frame["volume"] = 1000.0
        assert self.run(FakeSource(frame=frame)) == []

    def test_ml_leg_asks_datasource_for_feature_columns(self):
        """开启 ML 腿时必须把特征列需求下发给数据源，否则规则拿不到列会静默不判分。"""
        ds = FakeSource()
        cfg = self.risk_cfg()
        cfg["risk"]["ml"] = {"enabled": True}
        if not (Path(__file__).resolve().parent.parent / "data" / "models"
                / "risk_ex10.joblib").is_file():
            pytest.skip("模型未生成（data/ 不入库）")
        from stock_selector.core.selector import run_select

        run_select(datasource=ds, config=cfg)
        assert ds.prepared["rows"] >= 700
        assert "lu" in ds.prepared["columns"]

    def test_without_risk_section_behaviour_is_unchanged(self):
        """没配 risk: 时内核保持旧行为：不判风控、decision 字段留空。"""
        from stock_selector.core.selector import run_select

        results = run_select(datasource=FakeSource(), config=config())
        assert results and all(r.decision == "" for r in results)
