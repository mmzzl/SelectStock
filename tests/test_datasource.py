"""数据源实现测试：本地指标宽表源与 MongoDB 源（离线，使用假客户端）。"""

import pandas as pd
import pytest


class TestParquetSource:
    def make(self, **config):
        from stock_selector.datasource.parquet_source import ParquetSource

        return ParquetSource(config)

    def test_stock_list_from_file_names(self, provider_dir):
        ds = self.make(dir=provider_dir)
        assert ds.get_stock_list() == [{"code": "000001", "name": ""}, {"code": "000002", "name": ""}]

    def test_available_true_when_files_exist(self, provider_dir):
        assert self.make(dir=provider_dir).available() is True

    def test_available_false_on_empty_dir(self, tmp_path):
        assert self.make(dir=tmp_path).available() is False

    def test_get_kline_sorted_ascending(self, provider_dir):
        df = self.make(dir=provider_dir).get_kline("000001")
        assert [d.strftime("%Y-%m-%d") for d in df["date"]] == ["2026-09-01", "2026-09-02"]

    def test_get_kline_missing_code_returns_none(self, provider_dir):
        assert self.make(dir=provider_dir).get_kline("999999") is None

    def test_name_registered_after_load(self, provider_dir):
        ds = self.make(dir=provider_dir)
        assert ds.name_of("000001") == ""          # 未取数前无从得知
        ds.get_kline("000001")
        assert ds.name_of("000001") == "PingAn"

    def test_max_date_tracks_latest(self, provider_dir):
        ds = self.make(dir=provider_dir)
        assert ds.max_date() is None
        ds.get_kline("000001")
        assert ds.max_date() == "2026-09-02"

    def test_rows_window_keeps_tail(self, provider_dir):
        df = self.make(dir=provider_dir, rows=1).get_kline("000001")
        assert len(df) == 1
        assert df["date"].iloc[0].strftime("%Y-%m-%d") == "2026-09-02"

    def test_pruned_columns_still_carry_name(self, provider_dir):
        df = self.make(dir=provider_dir, columns=["date", "close"]).get_kline("000001")
        assert "close" in df.columns and "name" in df.columns
        assert "ma5" not in df.columns

    def test_prepare_widens_pruned_read(self, provider_dir):
        """内核下发的策略需求只放宽取数范围：列取并集、行数取最大。"""
        ds = self.make(dir=provider_dir, columns=["date", "close"], rows=10)
        ds.prepare(columns=["date", "close", "ma10"], rows=30)
        assert ds.columns == ["date", "close", "ma10"]
        assert ds.rows == 30

    def test_prepare_never_narrows_full_read(self, provider_dir):
        """未配置 columns 即读全列、rows=0 即不限行数，不能被策略声明缩小。"""
        ds = self.make(dir=provider_dir)
        ds.prepare(columns=["ma10"], rows=30)
        assert ds.columns is None and ds.rows == 0

    def test_declared_column_absent_from_file_falls_back_to_full_read(self, provider_dir):
        """声明了本文件不存在的列时退回全列读取，而不是让整只股票取数失败。"""
        ds = self.make(dir=provider_dir, columns=["date", "close"])
        ds.prepare(columns=["ma20"], rows=0)
        df = ds.get_kline("000001")
        assert "ma20" not in df.columns and "ma10" in df.columns

    def test_relative_dir_resolves_against_repo_root(self):
        ds = self.make(dir="data/stock_kline")
        assert ds.dir.is_absolute()
        assert ds.dir.name == "stock_kline"


class FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, key, direction):
        self._docs = sorted(self._docs, key=lambda d: d[key], reverse=direction < 0)
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    def __iter__(self):
        return iter(self._docs)


class FakeCollection:
    def __init__(self, docs):
        self.docs = docs
        self.queries = []

    def find(self, flt=None, projection=None):
        self.queries.append(flt or {})
        code = (flt or {}).get("code")
        rows = [d for d in self.docs if code is None or d.get("code") == code]
        return FakeCursor(rows)


class FakeDB(dict):
    def __getitem__(self, name):
        return self.get(name) or FakeCollection([])


class TestMongoSource:
    def make(self, db, **config):
        from stock_selector.datasource.mongo_source import MongoSource

        ds = MongoSource({"rows": 3, **config})
        ds._client = object()          # 阻断真实连接，_db 已被替换
        ds._db = lambda: db
        return ds

    def test_prepare_raises_on_indicator_columns(self):
        """指标类策略配 Mongo 源必须在开工前就报清楚，而不是每票 KeyError 静默 0 命中。"""
        ds = self.make(FakeDB())
        with pytest.raises(RuntimeError, match="parquet"):
            ds.prepare(columns=["date", "open", "high", "low", "close", "volume", "ma20"])

    def test_prepare_accepts_base_columns_and_widens_rows(self):
        ds = self.make(FakeDB())
        ds.prepare(columns=["date", "close", "amount"], rows=250)
        assert ds.rows == 250

    def test_normalize_code_variants(self):
        from stock_selector.datasource.mongo_source import normalize_code

        assert normalize_code("600519") == "600519"
        assert normalize_code("600519.SH") == "600519"
        assert normalize_code("SH600519") == "600519"
        assert normalize_code(None) == ""
        assert normalize_code("12345") == ""      # 位数不足不当股票代码

    def test_stock_list_dedups_and_maps_names(self):
        db = FakeDB(bk_stocks=FakeCollection([
            {"stock_code": "600519", "stock_name": "贵州茅台"},
            {"stock_code": "600519.SH", "stock_name": "贵州茅台"},
            {"stock_code": "000001", "stock_name": "平安银行"},
        ]))
        ds = self.make(db)
        assert ds.get_stock_list() == [
            {"code": "000001", "name": "平安银行"},
            {"code": "600519", "name": "贵州茅台"},
        ]

    def test_get_kline_ascending_with_numeric_types(self):
        db = FakeDB(stock_kline=FakeCollection([
            {"code": "600519", "date": "2026-09-23 15:00", "open": "10", "high": "11",
             "low": "9.5", "close": "10.8", "volume": "1000", "amount": "0"},
            {"code": "600519", "date": "2026-09-24 15:00", "open": "10.8", "high": "11.4",
             "low": "10.7", "close": "11.2", "volume": "1200", "amount": "0"},
            {"code": "600519", "date": "2026-09-22 15:00", "open": "9.8", "high": "10.2",
             "low": "9.7", "close": "10", "volume": "900", "amount": "0"},
        ]))
        df = self.make(db).get_kline("600519")
        assert [d.strftime("%Y-%m-%d") for d in df["date"]] == \
            ["2026-09-22", "2026-09-23", "2026-09-24"]
        assert df["close"].dtype == "float64"
        assert df["close"].iloc[-1] == pytest.approx(11.2)

    def test_rows_limits_to_latest_window(self):
        docs = [{"code": "600519", "date": f"2026-09-{d:02d} 15:00", "close": str(d),
                 "open": "1", "high": "1", "low": "1", "volume": "1", "amount": "0"}
                for d in range(1, 10)]
        db = FakeDB(stock_kline=FakeCollection(docs))
        df = self.make(db).get_kline("600519")
        assert len(df) == 3
        assert df["date"].iloc[-1].strftime("%Y-%m-%d") == "2026-09-09"

    def test_empty_result_returns_none(self):
        db = FakeDB(stock_kline=FakeCollection([]))
        assert self.make(db).get_kline("400000") is None

    def test_close_releases_client(self, monkeypatch):
        db = FakeDB(stock_kline=FakeCollection([]))
        ds = self.make(db)
        released = []
        ds._client = type("C", (), {"close": lambda self: released.append(True)})()
        ds.close()
        assert released == [True]
        assert ds._client is None
        ds.close()                              # 幂等
        assert released == [True]
