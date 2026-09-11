"""共享 pytest fixtures。"""

import pandas as pd
import pytest


def _write_stock(dirpath, code, name, rows):
    df = pd.DataFrame(rows)
    df["code"] = code
    df["name"] = name
    df["date"] = pd.to_datetime(df["date"])
    df.to_parquet(dirpath / f"{code}.parquet", index=False, compression="zstd")


@pytest.fixture
def provider_dir(tmp_path):
    """临时目录，含 000001（两日）、000002（仅 09-01，09-02 停牌）两只股票 parquet。"""
    _write_stock(tmp_path, "000001", "PingAn", [
        {"date": "2026-09-01", "open": 10, "high": 10.5, "low": 9.8, "close": 10.2,
         "volume": 1000, "amount": 10000, "ma5": 10, "ma10": 9.9, "ma60": 9.5,
         "adx14": 22, "last_close": 10.0},
        {"date": "2026-09-02", "open": 10.2, "high": 10.8, "low": 10.1, "close": 10.7,
         "volume": 1200, "amount": 12800, "ma5": 10.1, "ma10": 10.0, "ma60": 9.6,
         "adx14": 24, "last_close": 10.2},
    ])
    _write_stock(tmp_path, "000002", "Wanke", [
        {"date": "2026-09-01", "open": 7, "high": 7.3, "low": 6.9, "close": 7.1,
         "volume": 800, "amount": 5600, "ma5": 7, "ma10": 6.9, "ma60": 6.6,
         "adx14": 18, "last_close": 7.0},
    ])
    return tmp_path
