"""因子条件筛选的口径测试。

重点是三件容易错、错了整张表就变成噪声的事：
1. **出场重演与引擎一致**：止损/止盈/持有到期都按收盘价判，顺序是先止损后止盈；
2. **分位不污染原始列**：`pos60`/`pct`/`vol_ratio` 既是筛选特征也是判据要读的列，
   就地覆盖成 0~100 会把命中全判成不合格（同一年 20 只票 35 个命中 → 0）；
3. **裁剪后的切片与整表同源**：`iloc` 只带 `required_columns` 是 60 倍提速的前提，
   但它必须给出与整表逐日切片**完全一样**的命中，否则筛选就是在测另一个策略。
"""

import importlib.util
import sys

import numpy as np
import pandas as pd
import pytest

SCRIPT = "scripts/screen_signal_factors.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("screen_signal_factors", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["screen_signal_factors"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load_script()


class FakeStrategy:
    """只在最后一根为 True 的行出信号（required_columns 用真实策略那份清单）。"""

    def __init__(self, mask=None):
        self.mask = mask

    def required_columns(self):
        return ["date", "open", "high", "low", "close", "volume",
                "code", "pct", "pos60", "lu", "v_reversal", "vol_blast"]

    def required_rows(self):
        return 5

    def signal_at(self, df):
        if not bool(df["v_reversal"].iloc[-1]):
            return None
        return object()


def _panel(dates, n_codes=5):
    rows = []
    for i in range(n_codes):
        for d, close in zip(dates, [10.0 + i] * len(dates)):
            rows.append({
                "date": d, "code": f"{i:06d}", "close": close, "open": close,
                "high": close, "low": close, "volume": 1000 + i,
                "code_id": i, "pct": close, "pos60": 0.2, "lu": False,
                "v_reversal": False, "vol_blast": False, "fwd10": 0.0,
                "roc10": i, "gap_pct": 0.0, "vol_ratio": 1.0, "vol_z": 0.0,
                "vol_trend": 0.0, "body_ratio": 0.5, "upper_ratio": 0.1,
                "lower_ratio": 0.1, "adx14": 10.0 + i, "rsi14": 50.0,
            })
    df = pd.DataFrame(rows)
    for f in ("atr_pct", "ma5_gap", "ma20_gap", "liq"):
        df[f] = 0.1
    return df


# ---------------- 出场重演 ----------------

def test_exit_takes_stop_first_and_fills_at_that_close():
    # 第一天 -6% 触发止损：按当天收盘 -6% 出，不是 -5%（引擎按收盘价记成交）
    path = np.array([[-0.06, 0.20, 0.30]])
    assert mod.exit_pnl(path, 0.05, 0.10)[0] == pytest.approx(-0.06)


def test_exit_takes_profit_at_first_touch():
    path = np.array([[0.02, 0.11, 0.30]])
    assert mod.exit_pnl(path, 0.05, 0.10)[0] == pytest.approx(0.11)


def test_exit_both_hit_same_day_stop_wins():
    # 一天里既越过止损又越过止盈不可能（同涨同跌），但顺序必须固定：
    # 引擎先判止损，这里用 -6% 与 +11% 分两天验证优先级由数组顺序决定
    path = np.array([[-0.06, 0.11]])
    assert mod.exit_pnl(path, 0.05, 0.10)[0] == pytest.approx(-0.06)


def test_exit_holds_to_last_close_when_nothing_triggers():
    path = np.array([[0.01, 0.02, 0.03, 0.04]])
    assert mod.exit_pnl(path, 0.05, 0.10)[0] == pytest.approx(0.04)


def test_exit_zero_take_means_no_profit_leg():
    # 止盈=0 时一路涨到 +30% 也不卖，持有到期按最后收盘出（现役出场参数就是这个）
    path = np.array([[0.10, 0.20, 0.30]])
    assert mod.exit_pnl(path, 0.05, 0.00)[0] == pytest.approx(0.30)


def test_exit_skips_missing_days_and_settles_at_last_valid_close():
    # 第 3 天起停牌（NaN）：不能把 NaN 当成"没触发"一路算下去，也不能当 0
    path = np.array([[0.01, 0.02, np.nan, np.nan]])
    assert mod.exit_pnl(path, 0.05, 0.10)[0] == pytest.approx(0.02)


def test_exit_all_nan_stays_nan():
    path = np.array([[np.nan, np.nan]])
    assert np.isnan(mod.exit_pnl(path, 0.05, 0.10)[0])


# ---------------- 分位与原列 ----------------

def test_daily_ranks_do_not_overwrite_raw_columns():
    df = _panel([f"2026-01-{d:02d}" for d in range(1, 6)])
    df["pos60"] = 0.9                       # 判据要读的列：分位化后会变成 100，必须原样保留
    out = mod.add_daily_ranks(df)
    assert out["pos60"].tolist() == df["pos60"].tolist()
    assert "rk_pos60" in out.columns


def test_daily_ranks_are_within_day_cross_section():
    df = _panel(["2026-01-01", "2026-01-02"])
    out = mod.add_daily_ranks(df)
    day1 = out[out["date"] == "2026-01-01"]["rk_roc10"]
    assert sorted(day1.round(6)) == pytest.approx([20.0, 40.0, 60.0, 80.0, 100.0])


def test_rank_column_name_helper():
    assert mod.rank_col("pos60") == "rk_pos60"


# ---------------- 日级统计 ----------------

def test_day_stats_equal_weight_days_not_points():
    # 同一天 3 条小亏 + 另一天 1 条大赚：点级均值被扎堆那天主导，
    # 日等权应当把两天各算 1 票（-1% 与 +10% → +4.5%），不是点级的 (3*-1+10)/4
    dates = np.array(["2026-01-05"] * 3 + ["2026-01-06"])
    pnl = np.array([-0.01, -0.01, -0.01, 0.10])
    st = mod._day_stats(dates, pnl)
    assert st["exp_pp"] == pytest.approx(4.5)
    assert st["wr"] == pytest.approx(0.5)          # 两天各 0 胜 1 胜 → 0.5
    assert st["days_exp"] == 2


def test_day_stats_skip_days_without_both_sides_for_payoff():
    dates = np.array(["2026-01-05", "2026-01-05", "2026-01-06", "2026-01-06"])
    pnl = np.array([0.02, 0.03, -0.01, 0.04])   # 第一天全赢 → 赔率估不出来；第二天才有双边
    st = mod._day_stats(dates, pnl)
    assert st["days_payoff"] == 1
    assert st["payoff"] == pytest.approx(4.0)
    assert st["days_exp"] == 2                   # 期望与胜率用全部两天，不剔


# ---------------- 命中口径 ----------------

def test_signal_hits_matches_full_frame_slicing():
    """裁剪切片与整表逐日切片必须给出同一批命中。"""
    dates = [f"2026-01-{d:02d}" for d in range(1, 21)]
    panel = _panel(dates)
    mask = {d: (i % 3 == 0) for i, d in enumerate(dates[5:], start=5)}
    for d, on in mask.items():
        panel.loc[panel["date"] == d, "v_reversal"] = on
    strategy = FakeStrategy()
    # signal_hits 吃的是**已分位化**的面板（run() 里 add_daily_ranks 在前），
    # 所以契约是「先分位、再取命中」，缺列要立刻报错而不是悄悄当成没分位
    ranked = mod.add_daily_ranks(panel)
    hits = mod.signal_hits(strategy, ranked, "2026-01-01", "2026-01-20", 5, 10)

    keep = [c for c in dict.fromkeys([*strategy.required_columns(), *mod.SLICE_EXTRA])
            if c in panel.columns]
    expected = []
    for code, raw in panel.groupby("code", sort=False):
        full = raw.reset_index(drop=True)
        slim = raw[keep].reset_index(drop=True)
        for p in range(5, len(full) - mod.FWD):   # 最后 10 行没有前视收益，与 signal_hits 同口径
            if strategy.signal_at(full.iloc[p - 4: p + 1]) is not None:
                assert strategy.signal_at(slim.iloc[p - 4: p + 1]) is not None
                expected.append((str(full["date"].iloc[p]), code))
    assert sorted(zip(hits["date"], hits["code"])) == sorted(expected)


def test_signal_hits_emits_forward_close_path():
    dates = [f"2026-01-{d:02d}" for d in range(1, 21)]
    panel = _panel(dates)
    panel["close"] = 10.0
    for d in dates[5:]:
        panel.loc[panel["date"] == d, "v_reversal"] = True
    hits = mod.signal_hits(FakeStrategy(), mod.add_daily_ranks(panel),
                           "2026-01-01", "2026-01-20", 5, 3)
    assert len(hits) > 0
    assert {"r1", "r2", "r3"} <= set(hits.columns)
    # 价格不动 → 入场后每一天的相对收益都是 0
    assert hits[["r1", "r2", "r3"]].to_numpy().max() == 0.0


def test_signal_hits_drops_rows_without_forward_window():
    dates = [f"2026-01-{d:02d}" for d in range(1, 21)]
    panel = _panel(dates)
    for d in dates:
        panel.loc[panel["date"] == d, "v_reversal"] = True
    hits = mod.signal_hits(FakeStrategy(), mod.add_daily_ranks(panel),
                           "2026-01-01", "2026-01-20", 5, 3)
    # 最后 10 行没有 T+10 前视收益，按既定口径不参与
    assert hits["date"].max() < dates[-10]
