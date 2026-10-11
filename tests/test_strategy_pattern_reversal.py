"""低位反转策略的用例：命中四条件、两条否决，以及形态列缺失时的自算退路。

日线夹具按生产同序生成（指标 → add_base_columns → add_pattern_columns），
测的是 parquet 里真实长出来的那一套列。每条否决用例只改动一个条件，
避免"因为别的原因没命中"这种假通过。
"""

import pandas as pd
import pytest

from stock_selector.features.base_columns import add_base_columns
from stock_selector.features.patterns import PATTERN_COLUMNS, add_pattern_columns
from stock_selector.strategies.pattern_reversal import TRIGGER, PatternReversal


def bars_to_df(bars, code="600519") -> pd.DataFrame:
    df = pd.DataFrame(bars, columns=["open", "high", "low", "close", "volume"])
    df["date"] = pd.date_range("2024-01-01", periods=len(df), freq="B")
    df["code"] = code
    return df.reset_index(drop=True)


def build(bars, code="600519") -> pd.DataFrame:
    """复刻 build_kline_features.compute_indicators 的列生成顺序（只留策略用到的列）。"""
    df = bars_to_df(bars, code)
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["vol_ma5"] = df["volume"].rolling(5).mean()
    df["last_close"] = df["close"].shift(1)
    return add_pattern_columns(add_base_columns(df))


def bar(close, volume=1000.0):
    return (close + 0.05, close + 0.08, close - 0.10, close, volume)


def slide(n, start, end):
    return [bar(start - i * (start - end) / (n - 1)) for i in range(n)]


# 低位 V 型反弹：长阴腿 → 5 根加速杀到 10.4 → 3 根回升。
# 末根相对 12 根前 -14%（跌透）、相对 6 日低点 +14%（已确认）、pos60≈0.12。
CRASH = [bar(c) for c in (12.4, 11.6, 11.0, 10.6, 10.4)]
BOUNCE = [bar(c) for c in (10.9, 11.3, 11.7)]
BOTTOM = slide(56, 22.0, 13.0) + CRASH + BOUNCE


def strategy(**config) -> PatternReversal:
    return PatternReversal(config)


def test_declared_columns_exist_in_precomputed_table():
    """策略声明的形态列必须真在 parquet 宽表里，否则剪列读取会静默 0 命中。"""
    assert TRIGGER in PATTERN_COLUMNS and "vol_blast" in PATTERN_COLUMNS
    declared = set(strategy().required_columns())
    assert {"pos60", "lu", "pct", TRIGGER, "vol_blast"} <= declared
    # 被外部样本否掉的判据不再读列，防止悄悄加回来
    assert not {"ma5", "ma20", "vol_expanding", "vol_trend", "pullback_stabilize",
                "box_break_up", "neckline_sup"} & declared


def test_hits_live_v_reversal_at_low_position():
    df = build(BOTTOM)
    hit = strategy().signal_at(df)
    assert hit is not None and hit.signal == "pattern_reversal"
    assert bool(df[TRIGGER].iloc[-1]) and float(df["pos60"].iloc[-1]) <= 0.35
    assert hit.score == pytest.approx(0.6)
    assert "V型反转" in hit.reason


def test_silent_before_the_reversal_is_confirmed():
    """阴腿还在创新低、没回升 8% 就不发：v_reversal 是触发条件，不是加分项。"""
    df = build(slide(56, 22.0, 13.0) + CRASH)
    assert not bool(df[TRIGGER].iloc[-1])
    assert strategy().signal_at(df) is None


def test_silent_at_high_position():
    """一模一样的跌透+反弹长在 60 日区间上半段不买：形态看位置。"""
    df = build([bar(12.0 + i * 0.333) for i in range(60)]
               + [bar(24.2 - i * 0.8) for i in range(7)]
               + [bar(c) for c in (20.9, 21.3, 21.7, 22.0)])
    assert bool(df[TRIGGER].iloc[-1]) and float(df["pos60"].iloc[-1]) > 0.35
    assert strategy().signal_at(df) is None


def test_silent_on_blast_volume():
    """只把末根成交量放大到 z≥2.5，其余不动：当日爆量两个时代都是负超额。"""
    df = build(BOTTOM[:-1] + [bar(11.7, volume=9000.0)])
    assert bool(df["vol_blast"].iloc[-1]) and bool(df[TRIGGER].iloc[-1])
    assert strategy().signal_at(df) is None


def test_silent_on_limit_up():
    """末根 +10% 真涨停（600 开头）：买不进就不发，别指望次日溢价。"""
    df = build(slide(56, 22.0, 13.0) + [bar(c) for c in (12.2, 11.3, 10.6, 10.0)]
               + [bar(c) for c in (10.4, 10.8, 11.0, 12.1)])
    assert bool(df["lu"].iloc[-1]) and bool(df[TRIGGER].iloc[-1])
    assert strategy().signal_at(df) is None


def test_deleted_gates_stay_deleted():
    """MA5 与量能两条门槛在八年外部样本上是反号的，这里锁住"它们不参与判定"。"""
    df = build(BOTTOM).copy()
    last = df.index[-1]
    df.loc[last, "ma5"] = float(df["close"].iloc[last]) * 1.2     # 收在 MA5 下方
    df.loc[last, "vol_expanding"] = False
    df.loc[last, "vol_trend"] = 0.2                               # 量能萎缩
    assert strategy().signal_at(df) is not None


def test_works_on_slice_without_pattern_columns():
    """mongo/ak 源没有预计算列：策略要自己补算，且结论与读 parquet 一致。"""
    built = strategy().signal_at(build(BOTTOM))
    raw = strategy().signal_at(bars_to_df(BOTTOM))
    assert built is not None and raw is not None
    assert raw.score == pytest.approx(built.score)
    assert raw.reason == built.reason


def test_score_has_no_ranking_information():
    """命中就是同一档：位置深浅/反弹幅度/当日涨幅在两个时代都反号，score 不该假装有信息。"""
    shallow, deep = build(BOTTOM), build(slide(56, 34.0, 13.0) + CRASH + BOUNCE)
    a, b = strategy().signal_at(shallow), strategy().signal_at(deep)
    assert a is not None and b is not None
    pos_a = float(a.reason.split("pos60=")[1].split("，")[0])
    pos_b = float(b.reason.split("pos60=")[1].split("，")[0])
    assert pos_a != pos_b                                  # 位置确实不同
    assert a.score == pytest.approx(b.score) == 0.6        # 但排名不因此变化


def test_too_short_history_returns_none():
    """pos60 要 60 根K线才成立，不足就不判（而不是拿半个窗口当低位）。"""
    assert strategy().signal_at(build(BOTTOM[-58:])) is None
    assert strategy().signal_at(build(BOTTOM)) is not None


def test_max_pos60_is_configurable():
    df = build(BOTTOM)
    pos = float(df["pos60"].iloc[-1])
    assert strategy().signal_at(df) is not None
    assert strategy(max_pos60=pos - 0.01).signal_at(df) is None
