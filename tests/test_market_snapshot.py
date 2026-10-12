"""盘后快照脚本的口径测试：标的池、贴板判定、命中不足不落盘、按交易日落文件。"""

import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest

SCRIPT = "scripts/fetch_market_snapshot.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("fetch_market_snapshot", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fetch_market_snapshot"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load_script()


def _rec(code="600000", name="浦发银行", price=9.68, limit_up=10.49, limit_down=8.59,
         quote_time="20261012130400"):
    """tencent.parse() 出来的快照形状（脚本只消费这个字典，不碰网络）。"""
    return {
        "code": code, "name": name, "price": price, "prev_close": 9.54, "open": 9.55,
        "high": 9.72, "low": 9.50, "volume": 407851.0, "amount": 393954353.0,
        "outer_volume": 200000.0, "inner_volume": 207851.0, "bids": [(9.67, 5)],
        "asks": [(9.68, 538)], "quote_time": quote_time, "pct": 1.47,
        "turnover_pct": 0.12, "vol_ratio": 0.7, "float_mv": 3224.01, "total_mv": 3224.01,
        "limit_up": limit_up, "limit_down": limit_down, "avg_price": 9.66,
    }


def test_quote_date_uses_snapshot_session_day():
    assert mod._quote_date("20261012130400") == "2026-10-12"


def test_quote_date_falls_back_to_today_on_garbage():
    from datetime import datetime

    assert mod._quote_date("") == datetime.now().strftime("%Y-%m-%d")


def test_at_limit_up_needs_half_tick_tolerance():
    """价格是 0.01 一跳，贴板判定留半个 tick，否则 10.489 这类浮点会漏判。"""
    assert mod._at_limit(10.49, 10.49, True)
    assert mod._at_limit(10.485, 10.49, True)
    assert not mod._at_limit(10.48, 10.49, True)


def test_at_limit_down_and_missing_limit_price():
    assert mod._at_limit(8.59, 8.59, False)
    assert not mod._at_limit(8.60, 8.59, False)
    # 退市/停牌没有涨跌停价（解析成 None），不能当成「贴跌停」
    assert not mod._at_limit(0.51, None, False)
    assert not mod._at_limit(0.51, 0.0, False)


def test_universe_from_kline_dir(tmp_path, monkeypatch):
    (tmp_path / "600000.parquet").write_bytes(b"")
    (tmp_path / "510300.parquet").write_bytes(b"")
    (tmp_path / "notes.txt").write_bytes(b"")
    (tmp_path / "12345.parquet").write_bytes(b"")     # 非六位代码不进池
    monkeypatch.setattr(mod, "KLINE_DIR", tmp_path)
    assert mod.load_universe(None, None) == ["510300", "600000"]


def test_explicit_codes_are_padded_and_limited():
    assert mod.load_universe(["1", "600000"], None) == ["000001", "600000"]
    assert mod.load_universe(["1", "600000"], 1) == ["000001"]


def test_frame_tags_funds_and_computes_limit_flags():
    df = mod.to_frame({"600000": _rec(price=10.49), "510300": _rec("510300", price=4.372)},
                      "2026-10-12 15:00:00")
    assert list(df.columns) == mod.COLUMNS
    assert df.set_index("code")["asset"].to_dict() == {"600000": "stock", "510300": "fund"}
    assert df.loc[df["code"] == "600000", "at_limit_up"].iloc[0] == True   # noqa: E712
    assert df.loc[df["code"] == "600000", "date"].iloc[0] == "2026-10-12"


def test_build_refuses_partial_snapshot(tmp_path, monkeypatch):
    """命中率不足 = 行情源故障，必须不落盘，否则风控拿到半张表会误判「无涨跌停数据」。"""
    monkeypatch.setattr(mod, "fetch_universe",
                        lambda u, b, t: {"600000": _rec()})
    with pytest.raises(SystemExit) as exc:
        mod.build(["600000", "600001", "600002", "600003"], 200, 8.0, 0.6, tmp_path, 0)
    assert "拒绝写出残缺文件" in str(exc.value)
    assert list(tmp_path.glob("*.parquet")) == []


def test_build_writes_one_file_per_session_day(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "fetch_universe",
                        lambda u, b, t: {"600000": _rec(), "510300": _rec("510300")})
    out = mod.build(["600000", "510300"], 200, 8.0, 0.6, tmp_path, 250)
    assert out == {"date": "2026-10-12", "rows": 2, "ratio": 1.0}
    df = pd.read_parquet(tmp_path / "2026-10-12.parquet")
    assert df["code"].tolist() == ["510300", "600000"]        # 按代码排序，重跑幂等


def test_prune_keeps_latest_files(tmp_path):
    for day in ("20261008", "20261009", "20261012"):
        (tmp_path / f"{day}.parquet").write_bytes(b"")
    (tmp_path / "scratch.parquet").write_bytes(b"")
    mod.prune(tmp_path, 2)
    assert sorted(p.name for p in tmp_path.glob("*.parquet")) == \
        ["20261009.parquet", "20261012.parquet", "scratch.parquet"]


def test_prune_disabled_with_zero(tmp_path):
    (tmp_path / "20261008.parquet").write_bytes(b"")
    mod.prune(tmp_path, 0)
    assert (tmp_path / "20261008.parquet").exists()
