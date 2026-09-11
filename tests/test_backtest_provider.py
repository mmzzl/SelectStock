"""Parquet data provider tests (TDD)."""

import pytest


class TestParquetProvider:
    def test_dates_union_across_files(self, provider_dir):
        from stock_selector.backtest.provider import ParquetProvider

        p = ParquetProvider(provider_dir)
        assert p.dates() == ["2026-09-01", "2026-09-02"]

    def test_price_returns_close(self, provider_dir):
        from stock_selector.backtest.provider import ParquetProvider

        p = ParquetProvider(provider_dir)
        assert p.price("2026-09-01", "000001") == pytest.approx(10.2)

    def test_price_none_on_paused_day(self, provider_dir):
        from stock_selector.backtest.provider import ParquetProvider

        p = ParquetProvider(provider_dir)
        assert p.price("2026-09-02", "000002") is None

    def test_prices_all_codes_for_day(self, provider_dir):
        from stock_selector.backtest.provider import ParquetProvider

        p = ParquetProvider(provider_dir)
        px = p.prices("2026-09-02")
        assert set(px.keys()) == {"000001"}

    def test_prices_subset_codes(self, provider_dir):
        from stock_selector.backtest.provider import ParquetProvider

        p = ParquetProvider(provider_dir)
        # 09-01 两只都有
        px = p.prices("2026-09-01", ["000001", "000002"])
        assert set(px.keys()) == {"000001", "000002"}

    def test_rows_returns_full_indicator_row(self, provider_dir):
        from stock_selector.backtest.provider import ParquetProvider

        p = ParquetProvider(provider_dir)
        row = p.rows("2026-09-01", ["000001"]).get("000001")
        assert row["adx14"] == 22
        assert row["close"] == 10.2
        assert row["last_close"] == 10.0

    def test_rows_empty_for_paused(self, provider_dir):
        from stock_selector.backtest.provider import ParquetProvider

        p = ParquetProvider(provider_dir)
        assert p.rows("2026-09-02", ["000002"]) == {}
