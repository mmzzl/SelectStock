"""批次 H：用户的真实交易规则「今天涨得多 → 明天低开开盘买 → 涨了就卖 / 跌 5% 止损」。

口径逐条来自 2026-10-29 他本人的话（先否掉了「今天涨停、当天收盘卖」那一版）：
  ① 人口：今天涨 ≥+5%（「今天涨得多的」，涨停只是其中一部分）
  ② 买入：**明天只要下跌就买**，不看 −5%；**取开盘价**
  ③ 卖出：**哪天收盘价比买入价高就哪天卖**（收盘成交）
  ④ 兜底：**一直不涨 ⇒ 跌 5% 就卖**（挂在 买入价×0.95 的止损单，按该价成交）
  ⑤ 我替他补的唯一一个数：**持有上限 10 个交易日**（含买入那天），到期既没涨也没触止损
     就按当天收盘卖。行情不够、走不完这条规则的样本直接丢，不编造成交价。

同一天既触及止损线又收盘高于买入价时**止损优先**：日线看不出日内顺序，按亏钱的那条记，
免得把回测读数做高。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stock_selector_qlib.dip_rule import (
    MAX_HOLD,
    STOP_PCT,
    UP_MIN_CHG,
    rule_event_rows,
    rule_exit,
)

DAYS = pd.date_range("2024-01-01", periods=14)


def _s(vals):
    return np.asarray(vals, dtype="float64")


# --- 出场规则本体 -------------------------------------------------------------------------


def test_up_day_exits_at_that_close():
    """买入当天收盘就高于开盘 ⇒ 当天卖（买入价=那天开盘）。"""
    out = rule_exit(10.0, _s([9.9, 10.2]), _s([10.3, 10.5]))
    assert (out["reason"], out["days"], out["price"]) == ("up", 0, 10.3)


def test_stop_wins_over_up_same_day():
    """同日既触及 −5% 又收盘高于买入价 ⇒ 记止损，不记止盈（保守）。"""
    out = rule_exit(10.0, _s([9.40]), _s([10.60]))
    assert (out["reason"], out["days"]) == ("stop", 0)
    assert pytest.approx(out["price"]) == 9.5


def test_stop_price_is_the_line_not_the_closer_low():
    """止损按挂单价 买入价×0.95 成交，不是按当天更低的收盘价（跌穿也只在 0.95 那档出货）。"""
    out = rule_exit(10.0, _s([9.49, 8.10]), _s([9.49, 8.30]))
    assert out["days"] == 0
    assert pytest.approx(out["price"]) == 9.5


def test_close_equal_to_entry_is_not_up():
    """收盘正好等于买入价不算「涨了」：止盈判据是严格大于。"""
    out = rule_exit(10.0, _s([9.8, 9.8]), _s([10.0, 10.001]))
    assert (out["days"], out["reason"]) == (1, "up")


def test_flat_path_exits_at_cap():
    """一直在 −4% 附近横着 ⇒ 满 MAX_HOLD 天到期，按最后一根收盘卖。"""
    out = rule_exit(10.0, _s([9.6] * MAX_HOLD), _s([9.8] * MAX_HOLD))
    assert (out["reason"], out["days"], out["price"]) == ("expired", MAX_HOLD - 1, 9.8)


def test_unresolved_when_bars_run_out():
    """行情不够 MAX_HOLD 根又没触发 ⇒ reason=open、不给价（绝不拿最后一根假装有成交）。"""
    out = rule_exit(10.0, _s([9.6, 9.6, 9.6]), _s([9.7, 9.7, 9.7]))
    assert out["reason"] == "open"
    assert np.isnan(out["price"])


def test_up_on_the_fifth_bar():
    out = rule_exit(10.0, _s([9.7, 9.6, 9.7, 9.9, 10.2]), _s([9.8, 9.7, 9.8, 9.95, 10.4]))
    assert (out["reason"], out["days"], out["price"]) == ("up", 4, 10.4)


def test_constants_are_the_users_spec():
    assert pytest.approx(STOP_PCT) == -0.05     # 「只要跌 5% 就卖出」
    assert pytest.approx(UP_MIN_CHG) == 0.05    # 「今天涨得多的（比如 ≥+5%）」
    assert MAX_HOLD == 10                       # 我补的唯一一个数


# --- 事件行（T 涨 ≥+5% ⇒ T+1 开盘低于 T 收盘才买，之后走上面的出场规则）----------------------

# A：2024-01-06 收 10.6（+6%）⇒ 01-07 开盘 10.0 买入（低开 5.66%）；
#     01-07 收 9.90、01-08 收 9.95（都没超过 10.0，最低 9.95/9.70 也没破止损线 9.50）、
#     01-09 收 10.20 > 10.0 ⇒ 第 2 根止盈，卖在 10.20。
_A_CLOSE = [10.0, 10.0, 10.0, 10.0, 10.0, 10.6, 9.90, 9.95, 10.20, 10.10, 10.0, 10.0, 10.0, 10.0]
_A_OPEN = [10.0, 10.0, 10.0, 10.0, 10.0, 10.5, 10.0, 9.95, 10.05, 10.10, 10.0, 10.0, 10.0, 10.0]
_A_LOW = [9.9, 9.9, 9.9, 9.9, 9.9, 10.2, 9.95, 9.70, 10.00, 9.60, 9.9, 9.9, 9.9, 9.9]
# B：2024-01-06 只涨 4%（10.0→10.4，不到 +5%）⇒ 不进事件
_B_CLOSE = [10.0] * 5 + [10.4] + [10.3, 10.5, 10.4, 10.3, 10.3, 10.3, 10.3, 10.3]
_B_OPEN = [10.0] * 5 + [10.3, 10.25, 10.4, 10.3, 10.3, 10.3, 10.3, 10.3, 10.3]
_B_LOW = [9.9] * 5 + [10.1, 9.80, 10.2, 10.0, 9.9, 9.9, 9.9, 9.9, 9.9]


def _mk(code, close, open_, low):
    idx = pd.MultiIndex.from_product([DAYS, [code]], names=["datetime", "instrument"])
    return pd.DataFrame({"close": close, "open": open_, "low": low}, index=idx)


def _panel() -> pd.DataFrame:
    return pd.concat([_mk("SH600001", _A_CLOSE, _A_OPEN, _A_LOW),
                      _mk("SH600002", _B_CLOSE, _B_OPEN, _B_LOW)]).sort_index()


def test_only_up_5pct_signal_days_become_events():
    out = rule_event_rows(_panel())
    assert list(out.index.get_level_values("instrument").unique()) == ["SH600001"]
    assert len(out) == 1, out.to_string()


def test_entry_is_next_open_and_label_is_exit_over_entry():
    out = rule_event_rows(_panel())
    row = out.iloc[0]
    assert out.index.get_level_values("datetime")[0] == pd.Timestamp("2024-01-06")
    assert row["exec_date"] == pd.Timestamp("2024-01-07")
    assert pytest.approx(row["entry"], rel=1e-12) == 10.0
    assert pytest.approx(row["label"], rel=1e-12) == 10.20 / 10.0 - 1


def test_gap_up_next_day_is_not_an_event():
    """明天高开（开盘 ≥ 今收）⇒ 不买、不进事件：用户说的是「下跌了买入」。"""
    p = _panel().copy()
    p.loc[(pd.Timestamp("2024-01-07"), "SH600001"), "open"] = 10.65
    assert len(rule_event_rows(p)) == 0


def test_exit_components_are_carried_for_audit():
    out = rule_event_rows(_panel())
    row = out.iloc[0]
    assert pytest.approx(row["signal_chg"], rel=1e-6) == 10.6 / 10.0 - 1
    assert pytest.approx(row["gap"], rel=1e-6) == 10.0 / 10.6 - 1
    assert (row["reason"], row["days"], row["exit_price"]) == ("up", 2, 10.20)


def test_event_needs_a_closed_path():
    """样本末尾还没结算的事件要丢掉（没有出场价就没法当训练行）。"""
    p = _panel().copy()
    p.loc[pd.Timestamp("2024-01-13"), ("SH600001", "close")] = 12.0   # 倒数第二天 +20% 信号
    p.loc[pd.Timestamp("2024-01-13"), ("SH600001", "open")] = 11.9
    p.loc[pd.Timestamp("2024-01-13"), ("SH600001", "low")] = 11.5
    p.loc[pd.Timestamp("2024-01-14"), ("SH600001", "open")] = 11.0    # 次日低开买入
    p.loc[pd.Timestamp("2024-01-14"), ("SH600001", "low")] = 10.50    # 没破 11.0×0.95=10.45
    p.loc[pd.Timestamp("2024-01-14"), ("SH600001", "close")] = 10.90  # 也没超过 11.0
    out = rule_event_rows(p)
    assert pd.Timestamp("2024-01-13") not in set(out.index.get_level_values("datetime"))
    assert len(out) == 1, out.to_string()


def test_stop_path_label_is_exactly_minus_five_percent():
    """走止损的那笔：出场价 = 买入价×0.95 ⇒ 标签就是 −5%（还没扣手续费）。"""
    p = _panel().copy()
    p.loc[pd.Timestamp("2024-01-08"), ("SH600001", "low")] = 9.49     # 破止损线 9.50
    p.loc[pd.Timestamp("2024-01-08"), ("SH600001", "close")] = 9.30
    out = rule_event_rows(p)
    row = out.iloc[0]
    assert (row["reason"], row["days"]) == ("stop", 1)
    assert pytest.approx(row["label"], rel=1e-12) == STOP_PCT


def test_panel_with_instrument_first_index_is_reordered():
    """qlib 两个入口的层级顺序相反（`D.features` 是 instrument 在前），本体要自己统一。"""
    p = _panel().swaplevel().sort_index()
    assert p.index.names == ["instrument", "datetime"]
    out = rule_event_rows(p)
    assert out.index.names == ["datetime", "instrument"]
    assert len(out) == 1, out.to_string()
