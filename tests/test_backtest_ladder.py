"""阶梯加仓纪律（批次 T，docs §33）：四笔 40/30/20/10 + 间距 k×ATR + 止损挂最近一笔成交价。

用户 2026-10-28 点名的原话是「底仓 400 → 涨 10% 加 300 → 再加 200 → 再加 100，每一次加仓之后
5% 止损线全部挂钩本次加仓成交价」，随后把两把尺子改成比率：金额按目标成本的 40/30/20/10，
间距按 2×ATR 或 1.5×ATR。这个模块只写那条规则本身——不碰行情、不碰账户、不 import qlib，
回测读数和线上诊断共用同一个状态机，免得两边各算一份对不上。

同日既触止损又触加仓时按止损算（§33.4 写死的保守序），跳空高开在触发价之上时按**开盘**成交
（条件单挂在上面，跳空只会成交得更差，不许按触发价算）。
"""
import numpy as np
import pandas as pd
import pytest

from stock_selector.backtest.ladder import (WEIGHTS, add_amount, raise_stop,
                                            rung_price, simulate_ladder)


def _frame(rows):
    """ATR 复用 `features/chips.atr14`，而它按同一份「停牌/缺数不进统计」的规矩过一遍行，
    所以 bars 必须带 amount——没有成交额的日子本来就是停牌，不该喂进波动率。"""
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"]).assign(amount=1e7)


# 15 根 (10,11,9,10)：每根 TR=2 ⇒ 入场日 ATR14 正好 2.0，k=2.0 时下一档触发价 = 10+4 = 14.0
_FLAT = [(10.0, 11.0, 9.0, 10.0)] * 15
ENTRY_AT = 14


def test_四笔按目标成本的四成到一成切_第五笔不给():
    assert [add_amount(s, 100000.0) for s in (1, 2, 3, 4)] == [40000.0, 30000.0, 20000.0, 10000.0]
    # 阶梯走完了：第 5 笔是 0 而不是「再挤一点」，仓位不许超过目标成本
    assert add_amount(5, 100000.0) == 0.0


def test_比率之和等于一_否则整条阶梯会超配():
    assert sum(WEIGHTS) == pytest.approx(1.0)


def test_触发价等于最近成交价加k倍atr():
    assert rung_price(10.0, 2.0, k=2.0) == pytest.approx(14.0)
    assert rung_price(10.0, 2.0, k=1.5) == pytest.approx(13.0)


def test_atr判不了就不给触发价():
    # 0 或 NaN 都算「这只票这段算不出波动」，宁可不加仓也不编一个价出来
    assert rung_price(10.0, float("nan"), k=2.0) is None
    assert rung_price(10.0, 0.0, k=2.0) is None
    assert rung_price(float("nan"), 2.0, k=2.0) is None


def test_止损线只跟着成交价上移不回退():
    assert raise_stop(None, 10.0) == pytest.approx(9.5)          # 底仓就有线（−5%）
    assert raise_stop(9.5, 14.0) == pytest.approx(13.3)          # 加仓后抬到加仓价×0.95
    # 摊低（在更低价加仓）不许把已经抬起来的线放低——那是这条纪律的全部意义
    assert raise_stop(13.3, 12.0) == pytest.approx(13.3)


def test_盘中触及触发价就按触发价成交一笔():
    bars = _frame(_FLAT + [(13.0, 14.5, 12.9, 14.0)] + [(15.0, 15.2, 14.8, 15.0)] * 9)
    out = simulate_ladder(bars, entry_at=ENTRY_AT, target_cost=100000.0, k=2.0, hold=10)
    assert [f["price"] for f in out["fills"]] == [10.0, 14.0]
    assert [f["amount"] for f in out["fills"]] == [40000.0, 30000.0]
    assert out["exit_reason"] == "hold" and out["exit_price"] == 15.0
    assert out["stop_line"] == pytest.approx(13.3)               # 已跟着加仓价抬上来


def test_加仓后跌破止损线按线价整笔出清():
    bars = _frame(_FLAT + [(13.0, 14.5, 12.9, 14.0), (14.0, 14.2, 13.0, 13.2)]
                  + [(15.0, 15.2, 14.8, 15.0)] * 8)
    out = simulate_ladder(bars, entry_at=ENTRY_AT, target_cost=100000.0, k=2.0, hold=10)
    assert out["exit_reason"] == "stop"
    # 线价成交（13.30）而不是当日低点 13.00，也与收盘价 13.20 无关——线就是价
    assert out["exit_price"] == pytest.approx(13.3)
    assert out["exit_at"] == ENTRY_AT + 2


def test_跳空高开在触发价之上时按开盘成交():
    bars = _frame(_FLAT + [(15.0, 15.5, 14.9, 15.2)] + [(15.0, 15.2, 14.8, 15.0)] * 9)
    out = simulate_ladder(bars, entry_at=ENTRY_AT, target_cost=100000.0, k=2.0, hold=10)
    # 触发价 14.0 被跳空越过：真实成交在开盘 15.0，按 14.0 算就是把这条纪律写松了
    assert out["fills"][1]["price"] == pytest.approx(15.0)


def test_同日既触止损又触加仓时按止损算():
    bars = _frame(_FLAT + [(10.0, 14.5, 9.0, 12.0)] + [(15.0, 15.2, 14.8, 15.0)] * 9)
    out = simulate_ladder(bars, entry_at=ENTRY_AT, target_cost=100000.0, k=2.0, hold=10)
    assert out["exit_reason"] == "stop" and out["exit_price"] == pytest.approx(9.5)
    assert len(out["fills"]) == 1                                # 没加成


def test_算不出波动的票就停在底仓不加仓():
    bars = _frame([(10.0, 10.0, 10.0, 10.0)] * 15 + [(11.0, 12.0, 10.9, 11.5)] * 10)
    out = simulate_ladder(bars, entry_at=ENTRY_AT, target_cost=100000.0, k=2.0, hold=10)
    assert len(out["fills"]) == 1
    assert out["exit_reason"] == "hold"


def test_走满四档就不再往里加():
    # k=0.1 让每一天都够触发；没有上限的话 10 天会加 10 笔，仓位直接冲破目标成本
    up = [(float(p), float(p), float(p - 1), float(p)) for p in range(11, 21)]
    out = simulate_ladder(_frame(_FLAT + up), entry_at=ENTRY_AT,
                          target_cost=100000.0, k=0.1, hold=10)
    assert len(out["fills"]) == 4
    assert sum(f["amount"] for f in out["fills"]) == pytest.approx(100000.0)


def test_每一笔买入都单独计手续费():
    bars = _frame(_FLAT + [(13.0, 14.5, 12.9, 14.0)] + [(15.0, 15.2, 14.8, 15.0)] * 9)
    out = simulate_ladder(bars, entry_at=ENTRY_AT, target_cost=100000.0, k=2.0, hold=10)
    # 4 万 + 3 万 的本金，两笔各 0.05% ⇒ 投入端费用 35 元（阶梯多一笔就多一笔钱）
    assert out["buy_fees"] == pytest.approx((40000.0 + 30000.0) * 0.0005)
    assert out["invested"] == pytest.approx(70000.0 + 35.0)


def test_未来根数不够拿满十日时整条不给读数():
    bars = _frame(_FLAT + [(13.0, 14.5, 12.9, 14.0)] * 3)
    assert simulate_ladder(bars, entry_at=ENTRY_AT, target_cost=100000.0,
                           k=2.0, hold=10) is None


def test_入场日之前的历史不够算atr时不给读数():
    bars = _frame(_FLAT[:8] + [(13.0, 14.5, 12.9, 14.0)] * 10)
    assert simulate_ladder(bars, entry_at=8, target_cost=100000.0, k=2.0,
                           hold=10) is None


def test_收益是扣完费的净值口径():
    bars = _frame(_FLAT + [(13.0, 14.5, 12.9, 14.0)] + [(15.0, 15.2, 14.8, 15.0)] * 9)
    out = simulate_ladder(bars, entry_at=ENTRY_AT, target_cost=100000.0, k=2.0, hold=10)
    shares = 40000.0 / 10.0 + 30000.0 / 14.0
    expect = shares * 15.0 * (1 - 0.0015) / (70035.0) - 1.0
    assert out["ret_pct"] == pytest.approx(expect, abs=1e-9)
    assert np.isfinite(out["ret_pct"])


def test_止损阈值给零就是整条关掉():
    # C 归因臂要「阶梯加仓但不设止损」：阈值为 0 时线必须不存在，而不是退化成「跌破成交价就卖」
    dip = [(10.5, 10.6, 9.6, 10.0)]                      # 低到 9.6，带止损时会被 9.5 那条线放走
    bars = _frame(_FLAT + dip + [(13.0, 14.5, 12.9, 14.0)] + [(15.0, 15.2, 14.8, 15.0)] * 8)
    out = simulate_ladder(bars, entry_at=ENTRY_AT, target_cost=100000.0, k=2.0,
                          hold=10, stop_pct=0.0)
    assert out["stop_line"] is None
    assert out["exit_reason"] == "hold" and len(out["fills"]) == 2
