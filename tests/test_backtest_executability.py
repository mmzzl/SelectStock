"""组合回测引擎的成交可执行性测试（涨停买不进 / 一字板卖不掉 / 停牌押着等）。

这轮改动之前引擎只看收盘价就假定成交，Sharpe 和回撤里混着一笔「撮合假设的钱」；
判据本身在 `stock_selector/backtest/executability.py`，与逐笔扫描 `scripts/profit_scan.py`
同一真源，所以这里既测引擎的行为，也测两条链路用的是同一把尺子。
"""
import pytest


class FakeData:
    """daily: {date: {code: row}}；row 里没写的字段补「不拦单」的默认值。"""

    def __init__(self, daily):
        self.daily = daily

    def dates(self):
        return list(self.daily.keys())

    def price(self, date, code):
        rec = self.daily.get(date, {}).get(code)
        return rec["close"] if rec else None

    def row(self, date, code):
        rec = self.daily.get(date, {}).get(code)
        if rec is None:
            return None
        c = rec["close"]
        out = {"close": c, "open": rec.get("open", c),
               "high": rec.get("high", c * 1.02), "low": rec.get("low", c * 0.98)}
        if "pct" in rec:
            out["pct"] = rec["pct"]
        return out

    def prices(self, date, codes=None):
        d = self.daily.get(date, {})
        if codes is None:
            return {c: r["close"] for c, r in d.items()}
        return {c: d[c]["close"] for c in codes if c in d}


class BuyOnce:
    def __init__(self, buys=None, sells=None, adds=None):
        from stock_selector.backtest.models import Instruction, Order

        self.buys, self.sells, self.adds = buys or {}, sells or {}, adds or {}
        self.Order, self.Instruction = Order, Instruction

    def propose_buys(self, date, cash, prices):
        return [self.Order(code=c, amount=a, name=c) for c, a in self.buys.get(date, [])]

    def evaluate_holdings(self, date, holdings, prices):
        adds = self.adds.get(date, {})
        out = []
        for code in holdings:
            if code in self.sells.get(date, []):
                out.append(self.Instruction(side="sell", code=code))
            if code in adds:
                out.append(self.Instruction(side="add", code=code, amount=adds[code]))
        return out


def run(daily, buys=None, sells=None, adds=None, **kw):
    from stock_selector.backtest.engine import BacktestEngine

    opts = {"capital": 100_000, "max_positions": 3, "position_cap_pct": 1.0}
    opts.update(kw)
    return BacktestEngine(strategy=BuyOnce(buys, sells, adds), data=FakeData(daily), **opts).run()


def row(close, pct=None, high=None, low=None):
    return {"close": close, "pct": pct, "high": high, "low": low}


class TestBuySide:
    def test_limit_up_close_is_not_buyable(self):
        """收在涨停线上（主板 10%）⇒ 这笔放弃，且原因记进账。"""
        daily = {"d1": {"600000": row(11.0, pct=10.0)}, "d2": {"600000": row(11.2)}}
        res = run(daily, buys={"d1": [("600000", 20000)]})
        assert res.trades == []
        assert res.executability["buy_blocked"] == 1
        assert res.executability["reasons"] == {"buy:at_limit": 1}

    def test_sealed_board_reports_its_own_reason(self):
        daily = {"d1": {"600000": row(11.0, pct=10.0, high=11.0, low=11.0)}}
        res = run(daily, buys={"d1": [("600000", 20000)]})
        assert res.executability["reasons"] == {"buy:sealed": 1}

    def test_blowed_board_is_buyable(self):
        """炸板（涨幅没到线）按可成交处理，别把 9.0% 也当买不进。"""
        daily = {"d1": {"600000": row(10.9, pct=9.0)}, "d2": {"600000": row(11.0)}}
        res = run(daily, buys={"d1": [("600000", 20000)]})
        assert [t["side"] for t in res.trades] == ["buy"]
        assert res.executability["buy_blocked"] == 0

    def test_gem_has_its_own_limit_line(self):
        """创业板 20%：涨 12% 在主板是封死、在创业板只是普通大涨。"""
        daily = {"d1": {"300750": row(11.2, pct=12.0)}, "d2": {"300750": row(11.4)}}
        res = run(daily, buys={"d1": [("300750", 20000)]})
        assert [t["side"] for t in res.trades] == ["buy"]

    def test_missing_pct_falls_back_to_prev_close(self):
        """行里没 pct：按买入那天存下的昨收折算涨幅；第一天连昨收都没有 ⇒ 放行但记一笔。"""
        daily = {"d1": {"600000": {"close": 10.0}},          # 无 pct、无昨收：判不了
                 "d2": {"600000": {"close": 11.0}}}          # 无 pct，按昨折算出 +10%
        res = run(daily, buys={"d1": [("600000", 20000)]}, adds={"d2": {"600000": 10000}})
        assert res.executability["pct_unknown"] == 1
        assert [t["side"] for t in res.trades] == ["buy"]    # 加仓被拦下
        assert res.executability["reasons"] == {"add:at_limit": 1}

    def test_day_change_pct_prefers_the_pct_column(self):
        """pct 列优先，缺了才折算，两个都没有就返回 None（无从判定 ≠ 涨幅 0）。"""
        from stock_selector.backtest import executability

        assert executability.day_change_pct({"pct": 3.2, "close": 11.0}, 10.0) == 3.2
        assert executability.day_change_pct({"close": 11.0}, 10.0) == pytest.approx(10.0)
        assert executability.day_change_pct({"close": 11.0}, None) is None
        assert executability.day_change_pct({"close": None}, 10.0) is None

    def test_suspended_code_cannot_be_bought(self):
        daily = {"d1": {"600000": row(10.0, pct=1.0)}}       # d1 根本没有 600519 的行
        res = run(daily, buys={"d1": [("600519", 20000)]})
        assert res.trades == []
        assert res.executability["reasons"] == {"buy:suspended": 1}

    def test_add_is_checked_like_a_buy(self):
        """加仓也是付钱建仓：涨停板上一样加不进去。"""
        daily = {"d1": {"600000": row(10.0, pct=1.0)}, "d2": {"600000": row(11.0, pct=10.0)}}
        res = run(daily, buys={"d1": [("600000", 20000)]}, adds={"d2": {"600000": 10000}})
        assert [t["side"] for t in res.trades] == ["buy"]
        assert res.executability["reasons"] == {"add:at_limit": 1}


class TestSellSide:
    def test_one_way_board_defers_then_fills(self):
        """一字跌停卖不掉 ⇒ 仓位押着等，第二个有对手盘的日子成交。"""
        daily = {"d1": {"600000": row(10.0, pct=1.0)},
                 "d2": {"600000": row(9.0, pct=-10.0, high=9.0, low=9.0)},   # 一字跌停
                 "d3": {"600000": row(8.6)}}                                 # 有对手盘
        res = run(daily, buys={"d1": [("600000", 20000)]}, sells={"d2": ["600000"]})
        sells = [t for t in res.trades if t["side"] == "sell"]
        assert [t["date"] for t in sells] == ["d3"]
        assert sells[0]["price"] == 8.6                     # 按真正能成交那天的收盘
        assert res.executability["sell_blocked"] == 1
        assert res.executability["sell_wait_days_max"] == 1
        assert res.executability["sell_wait_by_code"] == {"600000": 1}   # 出掉了也要留痕
        assert res.executability["sell_pending"] == {}      # 已经出了，不欠着

    def test_suspension_keeps_the_position(self):
        daily = {"d1": {"600000": row(10.0, pct=1.0)},
                 "d2": {},                                   # 停牌：整只票没行
                 "d3": {"600000": row(9.5)}}
        res = run(daily, buys={"d1": [("600000", 20000)]}, sells={"d2": ["600000"]})
        assert [t["date"] for t in res.trades if t["side"] == "sell"] == ["d3"]
        assert res.executability["reasons"] == {"sell:suspended": 1}
        # 停牌那天的净值不能凭空变成 0：按最后一个 known 收盘价估（2000 股 × 10.0）
        d2 = [p for p in res.equity if p["date"] == "d2"][0]
        assert d2["positions_value"] == pytest.approx(20000.0)

    def test_deferred_sell_survives_without_a_new_instruction(self):
        """策略只在卖不掉那天响一次，之后由引擎自己接着找机会卖，不需要策略重复下指令。"""
        daily = {"d1": {"600000": row(10.0, pct=1.0)},
                 "d2": {"600000": row(9.0, pct=-10.0, high=9.0, low=9.0)},
                 "d3": {"600000": row(8.5, pct=-5.0, high=8.6, low=8.4)},
                 "d4": {"600000": row(8.4)}}
        res = run(daily, buys={"d1": [("600000", 20000)]}, sells={"d2": ["600000"]})
        assert [t["date"] for t in res.trades if t["side"] == "sell"] == ["d3"]

    def test_still_holding_at_the_end_is_reported(self):
        """跑完还押着的卖出必须报出来，不然仓位凭空消失没人知道。"""
        daily = {"d1": {"600000": row(10.0, pct=1.0)},
                 "d2": {"600000": row(9.0, pct=-10.0, high=9.0, low=9.0)}}
        res = run(daily, buys={"d1": [("600000", 20000)]}, sells={"d2": ["600000"]})
        assert [t["side"] for t in res.trades] == ["buy"]
        assert res.executability["sell_pending"] == {"600000": 1}


class TestSharedJudgment:
    def test_profit_scan_uses_the_same_constants(self):
        """两条链路必须共用同一把尺子，否则「逐笔过关、组合层不认」就成了口径问题。"""
        import importlib.util

        from stock_selector.backtest import executability

        spec = importlib.util.spec_from_file_location("profit_scan", "scripts/profit_scan.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.executability is executability
        assert mod.REPAIR_MAX == executability.REPAIR_MAX_DAYS

    def test_limit_tolerance_is_not_two_different_numbers(self):
        from stock_selector.backtest import executability
        from stock_selector.features.base_columns import board_limit_threshold

        line = board_limit_threshold("600000")           # 主板线（库内口径 9.8%）
        assert executability.buy_blocked("600000", line) == "at_limit"
        assert executability.buy_blocked("600000", line - executability.LIMIT_TOL_PP) == "at_limit"
        assert executability.buy_blocked("600000", line - executability.LIMIT_TOL_PP - 0.01) is None

    def test_vectorized_entry_gate_agrees_with_scalar_predicate(self):
        """逐笔扫描的向量化买点掩码与标量判据在随机数据上必须给同一个答案。"""
        import numpy as np

        from stock_selector.backtest import executability
        from stock_selector.features.base_columns import board_limit_threshold

        rng = np.random.default_rng(11)
        gap = rng.uniform(-12.0, 22.0, 400)      # 次日开盘跳空（%），故意跨过 10%/20% 两条线
        for code in ("600000", "300750"):
            limit = board_limit_threshold(code)
            # 扫描里的掩码就是这一行：跳空贴着涨停线 ⇒ 买不进
            vec = gap < limit - executability.LIMIT_TOL_PP
            scalar = np.array([executability.buy_blocked(code, g) is None for g in gap])
            assert np.array_equal(vec, scalar)

    def test_entry_gate_on_a_stub_block(self):
        """真调用 profit_scan.entry_gate：次日开盘顶在涨停线上的格子必须被拦下。"""
        import importlib.util

        import numpy as np

        from stock_selector.backtest import executability
        from stock_selector.features.base_columns import board_limit_threshold

        spec = importlib.util.spec_from_file_location("profit_scan", "scripts/profit_scan.py")
        ps = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ps)

        class Stub:
            pass

        blk = Stub()
        blk.cols = {"open": np.array([[10.0, 20.0], [11.0, 22.0], [11.0, 24.5]]),
                    "close": np.array([[10.0, 20.0], [10.5, 20.0], [10.0, 20.0]])}
        blk.limit = np.array([[board_limit_threshold(c) for c in ("600000", "300750")]])
        entry_px, can_buy, have = ps.entry_gate(blk)
        # 第 0 行：主板次日开盘 +10%（贴线买不进）、创业板 +10%（离 19.5% 还远）
        assert can_buy[0].tolist() == [False, True]
        assert can_buy[1].tolist() == [True, False]
        assert have[2].tolist() == [False, False]          # 最后一天没有次日 ⇒ 无从买入
        assert np.isnan(entry_px[2]).all()
        assert entry_px[0].tolist() == [11.0, 22.0]

    def test_one_way_board_definition_matches_the_grid_rule(self):
        """一字板判定：数组口径 (high != low) 与标量口径 is_one_way_board 一致。"""
        import numpy as np

        from stock_selector.backtest import executability

        rng = np.random.default_rng(5)
        high = rng.uniform(10, 20, 300)
        low = np.where(rng.random(300) < 0.3, high, high - 0.5)
        assert [executability.is_one_way_board(h, l)
                for h, l in zip(high, low)] == list(high == low)

    def test_missing_high_low_does_not_block_a_sell(self):
        """缺高低价时无从判定一字板，不能默认拦单（否则会凭空造出「卖不掉」）。"""
        from stock_selector.backtest import executability

        assert executability.sell_blocked(None, None) is None
        assert executability.sell_blocked(float("nan"), float("nan")) is None
