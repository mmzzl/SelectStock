import numpy as np
import pandas as pd
import pytest

from stock_selector.features.window import (
    assign_labels,
    build_dataset,
    clean_volume,
    compute_window_features,
)

FEATURES = [
    "pct_20d", "slope_20d", "pos_60d", "ma5_ma20", "vol_ratio_avg",
    "up_day_vol_ratio", "vol_slope", "vol_corr_pct", "drawdown_20d",
    "test_limit_cnt", "limit_up_early", "max_gain_3d",
]


def _mk(code, start=10.0, n=120, limup_days=(), vol=100.0):
    rng = np.random.default_rng(0)
    close = np.full(n, start)
    for i in range(1, n):
        if i in limup_days:
            close[i] = close[i - 1] * 1.10
        else:
            close[i] = close[i - 1] * (1 + rng.normal(0.0005, 0.01))
    vol_arr = np.full(n, vol)
    for d in limup_days:
        vol_arr[d] = vol * 2
    return pd.DataFrame({
        "code": code,
        "date": pd.date_range("2025-06-02", periods=n, freq="B"),
        "open": close, "high": close * 1.02, "low": close * 0.98, "close": close,
        "volume": vol_arr, "amount": vol_arr * close,
    })


def _mk_label(code, n=90, limup_days=(), gain=1.10):
    """Craft close prices with peak-then-drop so pos60 lands in [0.15,0.55]."""
    rng = np.random.default_rng(0)
    close = np.zeros(n)
    close[0] = 10.0
    for i in range(1, 35):
        close[i] = close[i - 1] * (1 + rng.normal(0.004, 0.005))
    for i in range(35, 55):
        close[i] = close[i - 1] * (1 + rng.normal(-0.012, 0.005))
    for i in range(55, n):
        if i in limup_days:
            close[i] = close[i - 1] * gain
        else:
            close[i] = close[i - 1] * (1 + rng.normal(0.001, 0.005))
    vol_arr = np.full(n, 100.0)
    for d in limup_days:
        vol_arr[d] = 200.0
    return pd.DataFrame({
        "code": code,
        "date": pd.date_range("2025-06-02", periods=n, freq="B"),
        "open": close, "high": close * 1.02, "low": close * 0.98, "close": close,
        "volume": vol_arr, "amount": vol_arr * close,
    })


def test_clean_volume_clip():
    df = _mk("600000", n=60)
    df.loc[30, "volume"] = 5000.0
    out = clean_volume(df)
    med = df["volume"].rolling(20).median().iloc[30]
    assert out["volume"].iloc[30] == pytest.approx(8 * med)
    assert out.loc[30, "volume"] == pytest.approx(
        8 * df["volume"].iloc[30] / 5000 * 100
    )


def test_window_features_columns_and_value():
    df = _mk("600000", n=140, limup_days=range(125, 133))
    feat = compute_window_features(df, "600000")
    assert set(FEATURES) <= set(feat.columns)
    tail = feat.iloc[126:133]
    assert (tail["limit_up_early"] >= 1).all()
    assert (tail["max_gain_3d"] > 0).all()


def test_window_features_nan_leading():
    df = _mk("600000", n=120)
    feat = compute_window_features(df, "600000")
    assert feat["pct_20d"].iloc[:19].isna().all()
    assert feat["pos_60d"].iloc[:59].isna().all()


def test_assign_labels_positive():
    df = _mk_label("600000", n=90, limup_days=range(80, 85))
    lab = assign_labels(df, "600000")
    t = 75
    assert lab["label"].iloc[t] == 1


def test_assign_labels_fuzzy_drop():
    df = _mk_label("600000", n=90, limup_days=(80, 81))
    lab = assign_labels(df, "600000")
    assert np.isnan(lab["label"].iloc[75])


def test_assign_labels_negative():
    df = _mk_label("600000", n=90)
    lab = assign_labels(df, "600000")
    assert lab["label"].iloc[75] == 0


def test_assign_labels_non_candidate():
    df = _mk_label("600000", n=90)
    df.loc[75, "close"] = df.loc[55, "close"] * 1.30
    lab = assign_labels(df, "600000")
    assert np.isnan(lab["label"].iloc[75])


def _write_parquet(dirpath, code, df):
    df.to_parquet(dirpath / f"{code}.parquet", index=False, compression="zstd")


def test_build_dataset(tmp_path):
    _write_parquet(tmp_path, "600000", _mk_label("600000", n=90, limup_days=range(80, 85)))
    _write_parquet(tmp_path, "300750", _mk_label("300750", n=90))
    ds = build_dataset(tmp_path)
    assert {"code", "date", "label"} <= set(ds.columns)
    assert set(FEATURES) <= set(ds.columns)
    assert set(ds["code"]) == {"600000", "300750"}
    assert set(ds["label"]) == {0, 1}
    assert len(ds) > 0
