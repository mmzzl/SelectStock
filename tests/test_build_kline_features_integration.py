import importlib.util
import numpy as np
import pandas as pd
import pytest

SCRIPT = "scripts/build_kline_features.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("build_kline_features", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _df(n=70):
    close = np.linspace(10, 12, n)
    df = pd.DataFrame(
        {
            "code": "603000",
            "name": "Test",
            "date": pd.date_range("2026-01-01", periods=n, freq="B"),
            "open": close, "high": close * 1.02, "low": close * 0.98,
            "close": close,
            "volume": np.full(n, 100.0),
            "amount": np.full(n, 1000.0),
        }
    )
    return df


def test_output_columns():
    mod = _load_script()
    assert {"pct", "lu", "vol_ratio", "pos60"} <= set(mod.OUTPUT_COLUMNS)


def test_compute_indicators_has_derived():
    mod = _load_script()
    df = _df()
    out = mod.compute_indicators(df)
    assert {"pct", "lu", "vol_ratio", "pos60"} <= set(out.columns)
    close = df["close"].to_numpy()
    assert out["pct"].iloc[3] == pytest.approx((close[3] / close[2] - 1) * 100)
    assert out["vol_ratio"].iloc[30] == pytest.approx(100.0 / 100.0)
    assert out["lu"].dtype == bool
    assert out["pos60"].dropna().between(0, 1).all()
