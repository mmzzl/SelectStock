"""形态/量能派生列的行为用例。

用合成K线构造每个形态的教科书样子，逐条断言命中；同时守住两条契约：
列清单与 dtype（parquet 只收 float64/bool）、以及 as-of 不前看未来。
"""

import numpy as np
import pandas as pd
import pytest

from stock_selector.features.patterns import (
    PATTERN_BOOL_COLUMNS,
    PATTERN_COLUMNS,
    PATTERN_FLOAT_COLUMNS,
    add_pattern_columns,
)


def make(bars: list[tuple[float, float, float, float, float]]) -> pd.DataFrame:
    """(open, high, low, close, volume) 列表 → 形态函数要的日线。"""
    df = pd.DataFrame(bars, columns=["open", "high", "low", "close", "volume"])
    df["date"] = pd.date_range("2024-01-01", periods=len(df), freq="D")
    return df.reset_index(drop=True)


def trend_leg(n: int, start: float, step: float, body: float = 0.02,
              volume: float = 1000.0) -> list[tuple[float, float, float, float, float]]:
    """一段匀速趋势：给形态测试提供高位/低位背景，实体很小以免误触反转形态。"""
    rows = []
    for i in range(n):
        c = start + i * step
        rows.append((c - body, c + body * 2, c - body * 2, c, volume))
    return rows


def compute(df: pd.DataFrame) -> pd.DataFrame:
    return add_pattern_columns(df.copy())


def test_column_contract_and_dtypes():
    df = compute(make(trend_leg(90, 20.0, -0.1) + [(11.0, 11.05, 10.4, 11.02, 2600)]))
    for col in PATTERN_BOOL_COLUMNS:
        assert df[col].dtype == bool, col
    for col in PATTERN_FLOAT_COLUMNS:
        assert df[col].dtype == float, col
    assert set(PATTERN_COLUMNS) <= set(df.columns)


def test_hammer_low_only_at_low_position():
    down_bar = (11.0, 11.06, 10.4, 11.04, 1000)       # 长下影、小实体、几乎无上影
    up_bar = (19.0, 19.06, 18.4, 19.04, 1000)         # 同形状，接在上涨末端即高位
    down = compute(make(trend_leg(90, 20.0, -0.1) + [down_bar]))
    up = compute(make(trend_leg(90, 10.0, 0.1) + [up_bar]))
    assert bool(down["hammer_low"].iloc[-1]) is True
    assert bool(up["hammer_low"].iloc[-1]) is False
    assert bool(up["hanging_man_high"].iloc[-1]) is True     # 同形状在高位读作上吊线


def test_shooting_star_at_high_position():
    up_bar = (19.0, 19.7, 18.98, 19.03, 1000)         # 长上影 + 下影不超过实体
    down_bar = (11.0, 11.7, 10.98, 11.03, 1000)       # 同形状出现在低位，不该算流星
    df = compute(make(trend_leg(90, 10.0, 0.1) + [up_bar]))
    assert bool(df["shooting_star_high"].iloc[-1]) is True
    low = compute(make(trend_leg(90, 20.0, -0.1) + [down_bar]))
    assert bool(low["shooting_star_high"].iloc[-1]) is False


def test_doji_splits_by_position_and_volume():
    quiet = compute(make(trend_leg(90, 20.0, -0.1) + [(11.0, 11.1, 10.9, 11.01, 1000)]))
    assert bool(quiet["doji_low"].iloc[-1]) is True
    assert bool(quiet["doji_high_blast"].iloc[-1]) is False

    loud = compute(make(trend_leg(90, 10.0, 0.1) + [(19.0, 19.08, 18.92, 19.01, 20000)]))
    assert bool(loud["doji_high"].iloc[-1]) is True
    assert bool(loud["vol_blast"].iloc[-1]) is True
    assert bool(loud["doji_high_blast"].iloc[-1]) is True      # 高位十字星 + 巨量 = 分歧巨大


def test_bullish_engulf_requires_volume():
    prev_down = (12.0, 12.1, 11.2, 11.3)            # 前一根阴线
    engulf = (11.2, 12.3, 11.15, 12.05)             # 大阳吞掉整个实体
    base = trend_leg(60, 20.0, -0.13)
    with_vol = compute(make(base + [prev_down + (1000,), engulf + (1600,)]))
    without = compute(make(base + [prev_down + (1000,), engulf + (1000,)]))
    assert bool(with_vol["bullish_engulf"].iloc[-1]) is True
    assert bool(without["bullish_engulf"].iloc[-1]) is False


def test_morning_star_and_evening_star():
    base = trend_leg(60, 20.0, -0.15)
    day1 = (12.4, 12.5, 11.6, 11.7)                 # 大阴
    day2 = (11.65, 11.8, 11.5, 11.68)               # 小实体星
    day3 = (11.9, 12.9, 11.85, 12.8)                # 大阳收进第一天实体上半
    df = compute(make(base + [day1 + (1000,), day2 + (900,), day3 + (1700,)]))
    assert bool(df["morning_star"].iloc[-1]) is True

    base_up = trend_leg(60, 12.0, 0.15)
    d1 = (20.6, 21.4, 20.5, 21.3)
    d2 = (21.35, 21.5, 21.2, 21.42)
    d3 = (21.1, 21.2, 20.2, 20.3)
    df2 = compute(make(base_up + [d1 + (1000,), d2 + (900,), d3 + (1700,)]))
    assert bool(df2["evening_star"].iloc[-1]) is True


def test_harami_needs_shrink_volume():
    base = trend_leg(60, 15.0, -0.05)
    big = (13.0, 13.9, 12.1, 12.2)
    small = (12.7, 12.9, 12.4, 12.6)
    df = compute(make(base + [big + (1500,), small + (700,)]))
    assert bool(df["harami"].iloc[-1]) is True
    loud = compute(make(base + [big + (1500,), small + (1500,)]))
    assert bool(loud["harami"].iloc[-1]) is False


def test_three_soldiers_and_three_crows():
    base = trend_leg(60, 14.0, -0.03)
    soldiers = [(12.5, 12.9, 12.4, 12.8), (12.8, 13.2, 12.7, 13.1), (13.1, 13.5, 13.0, 13.4)]
    vols = (1100, 1250, 1400)
    df = compute(make(base + [s + (v,) for s, v in zip(soldiers, vols)]))
    assert bool(df["three_soldiers"].iloc[-1]) is True

    base_up = trend_leg(60, 10.0, 0.03)
    crows = [(13.5, 13.6, 13.0, 13.1), (13.1, 13.2, 12.6, 12.7), (12.7, 12.8, 12.2, 12.3)]
    df2 = compute(make(base_up + [c + (v,) for c, v in zip(crows, vols)]))
    assert bool(df2["three_crows"].iloc[-1]) is True


def _w_bottom_bars(vol_break: float = 3000.0):
    """两个等高低点（间隔 20 根）+ 中间反弹高点，最后一根放量收在颈线上方。"""
    first_low = 10.0
    mid = [(10.0 + i * 0.4, 10.5 + i * 0.4, 9.9 + i * 0.4, 10.1 + i * 0.4, 1000) for i in range(10)]
    down = [(13.8 - i * 0.4, 14.0 - i * 0.4, 13.4 - i * 0.4, 13.5 - i * 0.4, 1000) for i in range(10)]
    second = [(10.2 - i * 0.02, 10.3 - i * 0.02, 9.95, 10.1 - i * 0.02, 1000) for i in range(10)]
    rebound = [(10.2 + i * 0.1, 10.35 + i * 0.1, 10.1, 10.3 + i * 0.1, 1000) for i in range(8)]
    neck = max(h for _, h, _, _, _ in mid + down + rebound)
    break_bar = (neck - 0.1, neck + 0.5, neck - 0.2, neck + 0.4, vol_break)
    lead = trend_leg(70, 16.0, -0.086)[:70]
    return lead + [(first_low + 0.1, first_low + 0.6, first_low - 0.1, first_low + 0.2, 1000)] \
        + mid + down + second + rebound + [break_bar], neck


def test_w_bottom_break_fires_once_at_neckline_cross():
    bars, neck = _w_bottom_bars()
    df = compute(make(bars))
    hits = df.index[df["w_bottom_break"]].tolist()
    assert hits == [len(bars) - 1]
    assert np.isfinite(df["neckline_sup"].iloc[-1])
    assert df["neckline_sup"].iloc[-1] == pytest.approx(neck)
    assert bool(df["vol_expanding"].iloc[-1]) is True


def test_w_bottom_needs_volume_confirmation():
    bars, _ = _w_bottom_bars(vol_break=1000.0)      # 同一根K线但没放量
    df = compute(make(bars))
    assert df.index[df["w_bottom_break"]].tolist() == []


def test_m_top_break_mirror():
    top = 20.0
    mid = [(19.8 - i * 0.4, 20.0 - i * 0.4, 19.3 - i * 0.4, 19.4 - i * 0.4, 1000) for i in range(10)]
    back_up = [(15.4 + i * 0.4, 15.7 + i * 0.4, 15.2 + i * 0.4, 15.5 + i * 0.4, 1000) for i in range(10)]
    second_top = [(19.6 + i * 0.02, 20.05, 19.4 + i * 0.02, 19.7 + i * 0.02, 1000) for i in range(8)]
    slide = [(19.5 - i * 0.15, 19.6 - i * 0.15, 19.0 - i * 0.16, 19.1 - i * 0.15, 1000) for i in range(6)]
    neck = min(l for _, _, l, _, _ in mid + back_up + second_top + slide)
    bars = trend_leg(70, 14.0, 0.086) + [(top - 0.3, top + 0.1, top - 0.5, top - 0.2, 1000)] \
        + mid + back_up + second_top + slide + [(neck + 0.1, neck + 0.2, neck - 0.6, neck - 0.5, 3000)]
    df = compute(make(bars))
    hits = df.index[df["m_top_break"]].tolist()
    assert hits == [len(bars) - 1]
    assert np.isfinite(df["neckline_res"].iloc[-1])


def anchors(total: int, points: list[tuple[int, float]], spread: float = 1.0,
            volume: float = 1000.0) -> list[tuple[float, float, float, float, float]]:
    """按 (序号, 低点) 锚点线性插值出一段日线：低点正好落在锚点上，高点=低点+spread。

    线性插值保证锚点两侧严格单调，局部极值只会出现在锚点位置，形态扫描才认得到肩/头。
    """
    xs = np.arange(total)
    lows = np.interp(xs, [p[0] for p in points], [p[1] for p in points])
    return [(lo + spread * 0.3, lo + spread, lo, lo + spread * 0.35, volume) for lo in lows]


def test_head_shoulders_bottom_break():
    # 左肩低点 10.0 / 头 9.0 / 右肩 10.2；两次反弹的最低点 12.0、11.5 → 高点 13.0、12.5
    # 颈线取更严格的一侧 13.0，所以最后一根必须放量收上 13.0 才算头肩底突破
    bars = anchors(46, [(0, 11.5), (5, 10.0), (10, 12.0), (20, 9.0), (25, 11.5),
                        (38, 10.2), (45, 11.8)])
    bars.append((12.9, 13.9, 12.8, 13.5, 4000))
    df = compute(make(bars))
    assert df.index[df["hs_bottom_break"]].tolist() == [len(bars) - 1]
    assert df["neckline_sup"].iloc[-1] == pytest.approx(13.0)
    assert df.index[df["w_bottom_break"]].tolist() == []  # 两肩与头高差过大，不该误判双底


def test_box_break_up_and_pullback_stabilize():
    box = [(10.0, 10.4, 9.7, 10.2, 1000)] * 34
    up = box + [(10.3, 10.9, 10.25, 10.8, 2600)]
    df = compute(make(up))
    assert bool(df["box_break_up"].iloc[-1]) is True

    # 回踩 20 日支撑后缩量收阳：先跌出支撑、再贴着它收阳
    fall = [(10.8 - i * 0.06, 10.9 - i * 0.06, 10.4 - i * 0.06, 10.5 - i * 0.06, 1000) for i in range(20)]
    stabilize = (10.45, 10.6, 9.65, 10.55, 500)
    df2 = compute(make(box + fall[:3] + [stabilize]))
    assert bool(df2["pullback_stabilize"].iloc[-1]) is True


def test_tri_break_up_requires_squeeze_and_volume():
    """对称三角形：30 根高点下移、低点上移（区间逐日收窄）+ 量能萎缩，再放量突破。"""
    wide = [(13.0, 13.4, 10.5, 12.0, 1000)] * 20
    narrow = []
    for k in range(30):
        hi, lo = 12.4 - 0.03 * k, 11.0 + 0.03 * k
        narrow.append(((hi + lo) / 2, hi, lo, (hi + lo) / 2, 1400 - 20 * k))
    bars = wide + narrow + [(12.9, 12.95, 12.6, 12.6, 3000), (13.0, 13.6, 12.9, 13.5, 3000)]
    df = compute(make(bars))
    assert bool(df["tri_break_up"].iloc[-1]) is True
    # 同一根K线若不放量，就不该算突破
    quiet = make(wide + narrow + [(12.9, 12.95, 12.6, 12.6, 800), (13.0, 13.6, 12.9, 13.5, 800)])
    assert bool(compute(quiet)["tri_break_up"].iloc[-1]) is False


def test_vol_diverge_and_volume_context():
    rising_vol = [(10.0 + i * 0.1, 10.2 + i * 0.1, 9.9 + i * 0.1, 10.1 + i * 0.1, 2000) for i in range(25)]
    new_high_low_vol = (12.6, 12.95, 12.55, 12.9, 800)
    df = compute(make(rising_vol + [new_high_low_vol]))
    assert bool(df["vol_diverge"].iloc[-1]) is True       # 价创新高、量能跟不上
    assert bool(df["vol_contracting"].iloc[-1]) is True
    assert df["vol_trend"].iloc[-1] < 1.0


def test_pattern_columns_are_asof_safe():
    """截断重算必须等于全量结果的前缀：任何列不许把未来写进过去。"""
    fixtures = [_w_bottom_bars()[0], _flag_bars() + [(11.70, 12.40, 11.68, 12.32, 2600)]]
    for bars in fixtures:
        full = compute(make(bars))
        for cut in (len(bars) // 2, len(bars) - 3, len(bars) - 1):
            head = make(bars[:cut])
            part = compute(head)
            for col in PATTERN_COLUMNS:
                a = full[col].iloc[:cut].to_numpy(dtype=float)
                b = part[col].to_numpy(dtype=float)
                assert np.allclose(a, b, equal_nan=True), f"{col} 在 cut={cut} 处前视未来"


def test_empty_and_missing_columns_pass_through():
    assert add_pattern_columns(pd.DataFrame()).empty
    df = pd.DataFrame({"close": [1.0, 2.0], "date": pd.date_range("2024-01-01", periods=2)})
    out = add_pattern_columns(df.copy())
    assert "hammer_low" not in out.columns


def _flag_bars():
    """旗杆（12 根涨 20%）→ 4 根缩量小实体整理 → 放量突破旗面高。"""
    pole = trend_leg(12, 10.0, 0.19, volume=1050)
    flag = [(11.82, 11.88, 11.74, 11.78, 500), (11.78, 11.84, 11.70, 11.74, 500),
            (11.74, 11.80, 11.66, 11.70, 500), (11.70, 11.78, 11.62, 11.66, 500)]
    return trend_leg(30, 7.0, 0.10, volume=1000) + pole + flag


def test_flag_breakout_compares_against_flag_high():
    """突破线必须是旗面自己的高点：比 12 日窗口高点会让「回撤中」和「创新高」互斥，整表恒 False。"""
    out = compute(make(_flag_bars() + [(11.70, 12.40, 11.68, 12.32, 2600)]))
    assert bool(out["flag_up_break"].iloc[-1]) is True
    assert bool(out["res20"].notna().iloc[-1])              # res20 仍是 12.13，旗形与 20 日新高是两回事

    quiet = compute(make(_flag_bars() + [(11.70, 12.40, 11.66, 12.32, 500)]))
    assert bool(quiet["flag_up_break"].iloc[-1]) is False   # 缩量突破不算


def test_arc_bottom_fires_once_per_episode():
    """圆弧底是事件列：vol_trend 反复穿越 1.0 会把状态打断，必须靠重火间隔压成每次弧底一根。"""
    rng = np.random.default_rng(7)
    walk = 10 * np.exp(np.cumsum(rng.normal(-0.0008, 0.02, 700)))
    out = compute(make([(c * 0.995, c * 1.01, c * 0.99, c, 1000 * (1 + rng.random() * 0.4))
                        for c in walk]))
    hits = np.flatnonzero(out["arc_bottom"].to_numpy())
    assert len(hits) >= 2
    assert np.diff(hits).min() >= 40
