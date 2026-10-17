"""批次 E 的组合层现金调度：只卡买入与加仓、暴跌日逐日放松、reserve=0 与现行为一致。

这些用例钉的是预注册 E2 的口径本身（README §6 批次 E），不是收益：
放多深、几天放完、重叠锚点取最深不叠加、卖出永不因为下限被拦——都在读数之前就该锁死。
"""

import pandas as pd
import pytest

from stock_selector.backtest.broker import Broker
from stock_selector.backtest.cash_policy import CRASH_MIN_STOCKS, CashPolicy, crash_dates


def policy(reserve_pct, tranches, triggers, days):
    return CashPolicy(reserve_pct=reserve_pct, tranches=tranches,
                      triggers=set(triggers), days=list(days))


DAYS = ["d1", "d2", "d3", "d4", "d5", "d6", "d7"]


def test_reserve_zero_is_never_binding():
    """基准那一趟：floor 恒为 0，等于现引擎不干预现金。"""
    p = policy(0.0, 2, {"d2"}, DAYS)
    assert [p.floor(d, 100_000) for d in DAYS] == [0.0] * 7
    assert p.active_days == []


def test_normal_day_holds_the_reserve_back():
    p = policy(0.2, 2, {"d4"}, DAYS)
    assert p.floor("d1", 100_000) == pytest.approx(20_000)


def test_trigger_schedule_releases_one_tranche_per_day():
    """锚点当天放 1/n，逐日累加，第 n 天完全放开；日程过完回到全额下限。"""
    p = policy(0.3, 3, {"d2"}, DAYS)
    floors = [p.floor(d, 100_000) for d in DAYS]
    # 锚点当天就放第一批（尾盘），第 3 天全放开，日程结束后回到全额下限
    assert floors == pytest.approx([30_000, 20_000, 10_000, 0.0, 30_000, 30_000, 30_000])
    # 放走过子弹的交易日：锚点当天起 3 天（d2/d3/d4）
    assert p.active_days == ["d2", "d3", "d4"]


def test_overlapping_anchors_take_the_deepest_and_do_not_stack():
    """两个锚点挨着：取最深那条日程，不叠成 2×L/n。"""
    p = policy(0.3, 3, {"d2", "d4"}, DAYS)
    released = {d: p.floor(d, 100_000) for d in DAYS}
    assert released["d4"] == pytest.approx(0.0)       # d2 的日程走到 k=3 已全放开
    assert released["d5"] == pytest.approx(10_000)    # 只剩 d4 的 k=2 ⇒ 放 2/3
    assert released["d6"] == pytest.approx(0.0)       # d4 的 k=3
    assert released["d7"] == pytest.approx(30_000)    # 两条日程都结束了


def test_floor_blocks_buy_and_counts_it_separately():
    """本来买得起、被预留拦下来的，要单独计数：那是调度挡的，不是账户没钱。"""
    b = Broker(capital=100_000, max_positions=5, position_cap_pct=0.9)
    b.cash_floor = 20_000
    assert b.buy("600001", "甲", "d1", 10.0, 70_000) == 7_000
    assert b.cash == pytest.approx(29_982.5)           # 只剩不到 3 万现金，其中 2 万是预留
    with pytest.raises(ValueError, match="预留"):
        b.buy("600002", "乙", "d1", 10.0, 15_000)
    assert b.floor_blocked == 1
    assert b.cash == pytest.approx(29_982.5)           # 被拦的那笔没有动账


def test_add_respects_the_same_floor():
    b = Broker(capital=100_000, max_positions=5, position_cap_pct=0.9)
    b.buy("600001", "甲", "d1", 10.0, 50_000)
    b.cash_floor = 40_000
    with pytest.raises(ValueError, match="预留"):
        b.add("600001", "甲", "d2", 10.0, 10_000)
    b.cash_floor = 0.0
    assert b.add("600001", "甲", "d2", 10.0, 10_000) == 1_000


def test_sell_is_never_blocked_by_the_floor():
    """下限只卡买入侧：预留现金的日子照样要能止损/到期清仓。"""
    b = Broker(capital=100_000)
    b.buy("600001", "甲", "d1", 10.0, 20_000)
    b.cash_floor = 90_000
    assert b.sell("600001", "d2", 12.0) > 0
    assert b.positions == {}


def test_genuinely_broke_still_reports_资金不足():
    b = Broker(capital=10_000)
    b.cash_floor = 2_000
    with pytest.raises(ValueError, match="资金不足"):
        b.buy("600001", "甲", "d1", 10.0, 50_000)
    assert b.floor_blocked == 0


def test_crash_dates_needs_both_the_drop_and_enough_stocks(tmp_path):
    """薄样本日的 −9% 不算暴跌：那是几十只票的中位数，不是全市场。"""
    table = pd.DataFrame({
        "date": pd.to_datetime(["2016-01-04", "2018-10-08", "2019-03-08", "2021-05-06"]),
        "mkt_med_pct": [-9.97, -3.0, -2.9, -5.0],
        "mkt_n": [37, 2_000, 2_000, CRASH_MIN_STOCKS - 1],
    })
    path = tmp_path / "market_state.parquet"
    table.to_parquet(path, index=False)
    triggers, days = crash_dates(path)
    assert triggers == {"2018-10-08"}                 # 恰好 −3% 算触发；薄样本与 −2.9% 都不算
    assert days == ["2016-01-04", "2018-10-08", "2019-03-08", "2021-05-06"]


# ---- 引擎接线：cash_policy 缺省时行为一字不变 ----
class FakeData:
    def __init__(self, daily):
        self.daily = daily

    def dates(self):
        return list(self.daily)

    def price(self, date, code):
        rec = self.daily.get(date, {}).get(code)
        return rec["close"] if rec else None

    def row(self, date, code):
        rec = self.daily.get(date, {}).get(code)
        if rec is None:
            return None
        c = rec["close"]
        return {"close": c, "high": rec.get("high", c * 1.02),
                "low": rec.get("low", c * 0.98), "pct": rec.get("pct", 2.0)}

    def prices(self, date, codes=None):
        d = self.daily.get(date, {})
        return {c: r["close"] for c, r in d.items()}


class AlwaysBuy:
    """每天都把候选全买满的策略：只用来观察引擎的现金闸口，不看任何判据。"""

    needs_market_prices = False

    def __init__(self):
        from stock_selector.backtest.models import Order
        self.Order = Order
        self.codes = ["600001", "600002", "600003", "600004", "600005"]

    def propose_buys(self, date, cash, prices):
        return [self.Order(code=c, amount=10_000, name=c) for c in self.codes]

    def evaluate_holdings(self, date, holdings, prices):
        return []


def _daily():
    base = {"600001": 10.0, "600002": 20.0, "600003": 30.0, "600004": 40.0, "600005": 50.0}
    return {date: {code: {"close": px * (1 + 0.05 * i)} for code, px in base.items()}
            for i, date in enumerate(["d1", "d2", "d3", "d4"])}


def test_engine_without_policy_leaves_the_floor_at_zero():
    from stock_selector.backtest.engine import BacktestEngine

    eng = BacktestEngine(AlwaysBuy(), FakeData(_daily()), capital=50_000,
                         max_positions=5, position_cap_pct=0.5)
    result = eng.run()
    assert eng.broker.cash_floor == 0.0
    assert result.executability["cash_floor_blocked"] == 0


def test_engine_sets_the_floor_from_the_policy_each_day():
    from stock_selector.backtest.engine import BacktestEngine

    seen = []

    class Spy(CashPolicy):
        def floor(self, date, total_value):
            seen.append((date, total_value))
            return super().floor(date, total_value)

    p = Spy(reserve_pct=0.4, tranches=2, triggers={"d1"}, days=["d1", "d2", "d3", "d4"])
    eng = BacktestEngine(AlwaysBuy(), FakeData(_daily()), capital=50_000,
                         max_positions=5, position_cap_pct=0.5, cash_policy=p)
    result = eng.run()
    assert [d for d, _ in seen] == ["d1", "d2", "d3", "d4"]
    assert seen[0][1] == pytest.approx(50_000)                       # 第一天总资产=本金
    assert p.log["d1"]["floor"] == pytest.approx(p.log["d1"]["base"] / 2)   # 锚点当天放第一批
    assert p.log["d2"]["floor"] == pytest.approx(0.0)                 # 第二批放完，全额放开
    assert p.log["d3"]["floor"] == pytest.approx(p.log["d3"]["base"])  # 日程结束，回到下限
    assert eng.broker.cash_floor == pytest.approx(p.log["d4"]["floor"])
    assert result.executability["cash_floor_blocked"] > 0             # 确实有单子被预留挡下来
