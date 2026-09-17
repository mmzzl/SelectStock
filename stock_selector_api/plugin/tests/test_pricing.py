"""test_pricing.py"""
import pandas as pd
import pytest

from portfolio.pricing import PricingError, latest_price_from_df, latest_price


def _df():
    return pd.DataFrame({
        "date": ["2026-09-15", "2026-09-16", "2026-09-17"],
        "close": [10.0, 10.5, 11.0],
        "open": [9.9, 10.1, 10.4],
    })


def test_latest_price_from_df_uses_row_le_day():
    assert latest_price_from_df(_df(), "2026-09-16") == 10.5


def test_latest_price_from_df_future_day_uses_last():
    assert latest_price_from_df(_df(), "2026-09-20") == 11.0


def test_latest_price_from_df_empty_raises():
    with pytest.raises(PricingError):
        latest_price_from_df(pd.DataFrame(columns=["date", "close"]), "2026-09-16")


def test_latest_price_falls_back_to_ak(monkeypatch):
    calls = []

    class FakeAKSource:
        def get_kline(self, code):
            calls.append(code)
            return pd.DataFrame({"date": ["2026-09-16"], "close": [42.5]})

    monkeypatch.setattr("portfolio.pricing.AKShareSource", FakeAKSource)
    assert latest_price("600000", "2026-09-17") == 42.5
    assert calls == ["600000"]
