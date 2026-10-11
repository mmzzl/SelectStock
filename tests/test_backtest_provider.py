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


class TestUniverseSampling:
    """--universe 必须是等距抽样：codes() 升序时"取前 N 个"会只剩最老编号的深市主板。"""

    def test_sample_spreads_across_boards(self):
        from scripts.backtest_main import _sampled_codes

        codes = ([f"000{i:03d}" for i in range(1, 400)]      # 深市主板（老编号，排序后全在最前）
                 + [f"002{i:03d}" for i in range(1, 400)]
                 + [f"300{i:03d}" for i in range(1, 400)]
                 + [f"600{i:03d}" for i in range(1, 400)]
                 + [f"688{i:03d}" for i in range(1, 200)])
        picked = _sampled_codes(codes, 300)
        assert len(picked) == 300 and len(set(picked)) == 300
        assert {c[:3] for c in picked} == {"000", "002", "300", "600", "688"}
        assert picked[0] == codes[0]
        # 旧写法 codes[:300] 只会剩最老编号的深市主板
        assert len({c[:3] for c in codes[:300]}) == 1

    def test_sample_is_deterministic_and_never_exceeds_n(self):
        from scripts.backtest_main import _sampled_codes

        codes = [f"{i:06d}" for i in range(1, 6632)]
        assert _sampled_codes(codes, 300) == _sampled_codes(codes, 300)
        assert len(_sampled_codes(codes, 7000)) == len(codes)
        assert len(_sampled_codes(codes, 5)) == 5
