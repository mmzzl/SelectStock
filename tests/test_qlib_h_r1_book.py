"""`scripts/qlib_h_r1_backtest_ir.py` 的账本算术：日收益链要能乘回逐笔净收益，槽位账本绝不加杠杆。

这里盯的是四个会把 IR 静默做假的点：
① 同日进出（实测占多数）必须只出一根、且双边成本都在那根上；
② 跨天链的每一根都能乘回「出场/买入」的费后乘数；
③ 停牌那根没有价 ⇒ 不编造收益，走势并到下一根，总账不丢；
④ **槽位**：只有 1/S 的资金在仓位里，一天只有 1 只持仓时它的 +13% 只能给组合带来 +13%/S
   （第一版账本按「当日持仓等权」摊，等于让那天满仓押这一只，2024-09-30 一天就把整条曲线抬飞）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.qlib_h_r1_backtest_ir import (BUY_COST, SELL_COST, position_returns,  # noqa: E402
                                           slot_book)

D1 = pd.Timestamp("2024-01-02")
D2 = pd.Timestamp("2024-01-03")
D3 = pd.Timestamp("2024-01-04")
DAYS = pd.DatetimeIndex([D1, D2, D3])


def _closes(mapping: dict[str, list[float]]) -> pd.DataFrame:
    return pd.DataFrame(mapping, index=DAYS)


def _day_ix() -> dict:
    return {d: i for i, d in enumerate(DAYS)}


def _ev(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


NET = lambda gross: (1.0 + gross) * (1.0 - BUY_COST) * (1.0 - SELL_COST) - 1.0  # noqa: E731


def test_same_bar_exit_books_one_row_with_both_costs():
    """买入日即出场日：只有一行，收益 = 出场/开盘，双边成本都落在这一行。"""
    ev = _ev([{"instrument": "SH600001", "exec_date": D1, "exit_date": D1,
               "entry": 10.0, "exit_price": 10.5}])
    daily = position_returns(_day_ix(), _closes({"SH600001": [10.2, 11.0, 11.0]}), ev)
    assert len(daily) == 1
    assert daily.loc[0, "date"] == D1
    assert daily.loc[0, "ret"] == pytest.approx(NET(0.05))


def test_multi_bar_chain_compounds_to_the_net_event_return():
    """跨三根：逐根乘回去 = 出场/买入按费后市值复利（成本分别在首末两根）。"""
    ev = _ev([{"instrument": "SH600001", "exec_date": D1, "exit_date": D3,
               "entry": 10.0, "exit_price": 11.0}])
    daily = position_returns(_day_ix(), _closes({"SH600001": [10.2, 10.6, 11.0]}), ev)
    assert daily["ret"].tolist() == pytest.approx(
        [10.2 / 10.0 * (1 - BUY_COST) - 1.0, 10.6 / 10.2 - 1.0,
         11.0 / 10.6 * (1 - SELL_COST) - 1.0])
    total = float((1.0 + daily["ret"]).prod() - 1.0)
    assert total == pytest.approx(NET(11.0 / 10.0 - 1.0))


def test_suspended_bar_invents_no_return_and_keeps_the_total():
    """中间一根停牌（NaN）：那天不产生收益，走势并到下一根，总账不变。"""
    ev = _ev([{"instrument": "SH600001", "exec_date": D1, "exit_date": D3,
               "entry": 10.0, "exit_price": 12.0}])
    daily = position_returns(_day_ix(), _closes({"SH600001": [10.0, np.nan, 12.0]}), ev)
    assert daily["date"].tolist() == [D1, D3]          # D2 没有行 = 那天这仓位不占权重
    assert float((1.0 + daily["ret"]).prod() - 1.0) == pytest.approx(NET(0.20))


def test_entry_bar_with_no_close_still_charges_the_buy_cost_once():
    """买入日收盘判不出：第一根有效价承担「开盘→该价」的涨幅并扣一次买成本。"""
    ev = _ev([{"instrument": "SH600001", "exec_date": D1, "exit_date": D3,
               "entry": 10.0, "exit_price": 10.4}])
    daily = position_returns(_day_ix(), _closes({"SH600001": [np.nan, 10.2, 11.0]}), ev)
    assert daily["date"].tolist() == [D2, D3]
    assert daily["ret"].tolist()[0] == pytest.approx(10.2 / 10.0 * (1 - BUY_COST) - 1.0)
    assert daily["ret"].tolist()[1] == pytest.approx(10.4 / 10.2 * (1 - SELL_COST) - 1.0)


def test_slot_book_scales_a_single_holding_by_its_slot_not_the_whole_nav():
    """只有 1 只持仓、它当天 +10%：10 槽的账本当天只涨 1%，不是 10%（第一版就是错在这里）。"""
    daily = pd.DataFrame([{"date": D1, "pos": 0, "ret": 0.10}])
    ev = _ev([{"instrument": "SH600001", "exec_date": D1, "exit_date": D1}])
    out = slot_book(daily, ev, DAYS, slots=10, picks={D1: [0]})
    assert out["ret"].loc[D1] == pytest.approx(0.01)
    assert out["ret"].loc[D2] == 0.0
    assert out["used"].loc[D1] == pytest.approx(0.10)


def test_slot_ne_frees_the_day_after_the_exit_and_buys_the_next_signal():
    """出场那根收盘才腾出槽位：第二天开盘才有空槽接新信号。"""
    daily = pd.DataFrame([
        {"date": D1, "pos": 0, "ret": 0.02},          # pos0 在 D1 出场
        {"date": D2, "pos": 1, "ret": 0.05},          # pos1 也在 D2 出场
    ])
    ev = _ev([{"instrument": "SH600001", "exec_date": D1, "exit_date": D1},
              {"instrument": "SH600002", "exec_date": D2, "exit_date": D2}])
    out = slot_book(daily, ev, DAYS, slots=1, picks={D1: [0], D2: [1]})
    assert out["ret"].loc[D1] == pytest.approx(0.02)
    assert out["ret"].loc[D2] == pytest.approx(0.05)
    assert out["skipped"] == 0
    assert out["conc"].tolist() == [1.0, 1.0, 0.0]


def test_full_book_skips_the_signal_it_cannot_fund():
    """槽位满了就不买：多出来的信号记账 `skipped`，绝不隐含加杠杆。"""
    daily = pd.DataFrame([
        {"date": D1, "pos": 0, "ret": 0.01}, {"date": D2, "pos": 0, "ret": 0.01},
        {"date": D1, "pos": 1, "ret": 0.02}, {"date": D2, "pos": 1, "ret": 0.02},
    ])
    ev = _ev([{"instrument": "SH600001", "exec_date": D1, "exit_date": D2},
              {"instrument": "SH600002", "exec_date": D1, "exit_date": D2}])
    out = slot_book(daily, ev, DAYS, slots=1, picks={D1: [0, 1]})
    assert out["skipped"] == 1
    assert out["ret"].loc[D1] == pytest.approx(0.01)      # 只有 pos0 那份钱
    assert out["conc"].loc[D1] == 1.0
    assert out["used"].max() <= 1.0 + 1e-12


def test_weights_never_exceed_one_even_when_every_slot_is_filled():
    """满槽 = 资金占用 100%，任何一天的组合收益都不会超过 Σ 单只收益（不放大）。"""
    daily = pd.DataFrame([{"date": D1, "pos": k, "ret": 0.03} for k in range(3)])
    ev = _ev([{"instrument": f"SH60000{k}", "exec_date": D1, "exit_date": D1}
              for k in range(3)])
    out = slot_book(daily, ev, DAYS, slots=3, picks={D1: [0, 1, 2]})
    assert out["used"].loc[D1] == pytest.approx(1.0)
    assert out["ret"].loc[D1] == pytest.approx(0.03)      # 三槽各 1/3、各 +3%
