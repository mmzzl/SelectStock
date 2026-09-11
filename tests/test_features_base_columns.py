import numpy as np
import pandas as pd
import pytest

from stock_selector.features.base_columns import (
    add_base_columns,
    board_limit_threshold,
    is_limit_up,
)


def test_board_threshold_main():
    assert board_limit_threshold("600519") == 9.8
    assert board_limit_threshold("000001") == 9.8
    assert board_limit_threshold("002594") == 9.8


def test_board_threshold_gem_kcb():
    for code in ("300750", "301236", "688256"):
        assert board_limit_threshold(code) == 19.5


def test_board_threshold_bj():
    for code in ("830799", "430047", "920001"):
        assert board_limit_threshold(code) == 29.5


def test_board_threshold_default():
    assert board_limit_threshold("999999") == 9.8


def test_is_limit_up_boundary():
    assert is_limit_up("300750", 19.6)
    assert not is_limit_up("600519", 9.7)
    assert is_limit_up("600519", 9.8)


def test_add_base_columns():
    n = 65
    close = np.linspace(10.0, 13.0, n)
    volume = np.full(n, 100.0)
    volume[30] = 1000.0
    df = pd.DataFrame(
        {
            "code": "603000",
            "date": pd.date_range("2026-01-01", periods=n, freq="B"),
            "close": close,
            "volume": volume,
            "vol_ma5": pd.Series(volume).rolling(5).mean().to_numpy(),
        }
    )
    out = add_base_columns(df.copy())
    assert {"pct", "lu", "vol_ratio", "pos60"} <= set(out.columns)
    expected_pct = (pd.Series(close).pct_change() * 100).to_numpy()
    np.testing.assert_allclose(out["pct"].to_numpy(), expected_pct, equal_nan=True)
    assert out["lu"].dtype == bool
    expected_vr = pd.Series(volume) / pd.Series(volume).rolling(5).mean()
    np.testing.assert_allclose(out["vol_ratio"].to_numpy(), expected_vr.to_numpy(), equal_nan=True)
    assert out["pos60"].iloc[:59].isna().all()
    assert out["pos60"].iloc[59:].between(0, 1).all()
    assert {"code", "date", "close", "volume"} <= set(out.columns)
