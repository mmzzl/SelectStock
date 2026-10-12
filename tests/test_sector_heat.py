"""板块热力图预计算的核心口径测试（窗口切法、等权聚合、成交额兜底）。"""

import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest

SCRIPT = "scripts/build_sector_heat.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("build_sector_heat", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["build_sector_heat"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load_script()


def _kline(code, name, closes, start="2026-09-01"):
    """构造 collect_klines 出来的标准帧形状（成交额已是 turnover 列）。"""
    dates = pd.date_range(start, periods=len(closes), freq="B")
    df = pd.DataFrame({
        "code": code, "name": name, "date": dates,
        "close": closes,
        "pct": [0.0] + [(c / p - 1) * 100 for c, p in zip(closes[1:], closes[:-1])],
        "lu": False,
        "turnover": [1e6] * len(closes),
    })
    return df


def _frame(rows):
    return pd.concat(rows, ignore_index=True)


BOARDS = pd.DataFrame([
    {"kind": "industry", "board_code": "J66", "board_name": "货币金融服务", "stock_code": "000001"},
    {"kind": "industry", "board_code": "J66", "board_name": "货币金融服务", "stock_code": "000002"},
    {"kind": "industry", "board_code": "J66", "board_name": "货币金融服务", "stock_code": "600000"},
])


def test_window_is_trading_days_not_calendar_days():
    """窗口按交易日切：近 3 日 = 最后 3 根 K 线，基准是第 4 根收盘（跨周末不该少算一天）。"""
    klines = _frame([_kline("000001", "平安银行", [10, 10, 10, 10, 11, 11, 12])])
    sessions = sorted(klines["date"].unique())
    stats = mod.window_stats(klines, sessions, 3)
    assert stats.iloc[0]["days"] == 3
    assert round(stats.iloc[0]["ret_pct"], 4) == round((12 / 10 - 1) * 100, 4)


def test_suspended_stock_without_rows_in_window_is_dropped():
    live = _kline("000001", "平安银行", [10, 11, 12, 13, 14])
    dead = _kline("000002", "万科A", [10, 10, 10, 10, 10])
    dead["date"] = dead["date"] - pd.Timedelta(days=40)   # 整段窗口都停牌
    stats = mod.window_stats(_frame([live, dead]), sorted(pd.concat([live, dead])["date"].unique()), 3)
    assert stats["stock_code"].tolist() == ["000001"]


def test_stock_needing_base_row_is_excluded():
    """窗口内只有两根 K 线、窗口前没有收盘价的新股，区间涨幅无从算起，不该出现在热力图里。"""
    live = _kline("000001", "平安银行", [10, 10, 10, 10, 10, 10, 12, 12])
    ipo = _kline("301999", "新股", [10, 11], start="2026-09-09")
    klines = _frame([live, ipo])
    stats = mod.window_stats(klines, sorted(klines["date"].unique()), 3)
    assert "301999" not in set(stats["stock_code"])
    assert "000001" in set(stats["stock_code"])


def test_board_aggregates_equal_weight():
    klines = _frame([_kline("000001", "平安银行", [10, 10, 10, 10, 11, 11, 11]),
                     _kline("000002", "万科A", [10, 10, 10, 10, 10, 12, 14])])
    sessions = sorted(klines["date"].unique())
    stats = mod.window_stats(klines, sessions, 3)
    board, member = mod.aggregate_boards(BOARDS, stats, 3, sessions[-1])
    row = board.iloc[0]
    # 000001 收在 11（基准 10 → +10%），000002 收在 14（基准 10 → +40%），等权 +25%
    assert round(row["ret_pct"], 4) == 25.0
    assert row["up_count"] == 2 and row["down_count"] == 0
    assert row["leader_code"] == "000002" and row["lagger_code"] == "000001"
    assert len(member) == 2


def test_member_count_keeps_boards_without_kline_data():
    """member_count 是板块成分数、stock_count 是真有数据能算的只数，两者分开前端才知覆盖率。"""
    klines = _frame([_kline("000001", "平安银行", [10, 10, 10, 10, 11, 11, 11])])
    sessions = sorted(klines["date"].unique())
    board, _ = mod.aggregate_boards(BOARDS, mod.window_stats(klines, sessions, 3), 3, sessions[-1])
    assert int(board.iloc[0]["member_count"]) == 3
    assert int(board.iloc[0]["stock_count"]) == 1


def _raw_kline(closes, amount=None, volume=None):
    """爬虫落盘的原始日线形状（read_stock_frame 的输入，没有 code 列）。"""
    n = len(closes)
    return pd.DataFrame({
        "date": pd.date_range("2026-09-01", periods=n, freq="B"),
        "name": "平安银行",
        "close": closes,
        "pct": [0.0] + [(c / p - 1) * 100 for c, p in zip(closes[1:], closes[:-1])],
        "volume": volume or [1000.0] * n,
        "amount": amount or [0.0] * n,
        "lu": False,
    })


def test_turnover_falls_back_to_volume_close_when_amount_zero(tmp_path):
    """amount 自 2026-05-21 起断供全是 0，成交额按 volume(手)×close×100 估算。"""
    path = tmp_path / "000001.parquet"
    _raw_kline([10.0, 12.0]).to_parquet(path, index=False)
    out = mod.read_stock_frame(path)
    assert out["turnover"].tolist() == [1000 * 10 * 100, 1000 * 12 * 100]


def test_turnover_uses_real_amount_when_present(tmp_path):
    path = tmp_path / "000001.parquet"
    _raw_kline([10.0, 12.0], amount=[1e8, 2e8]).to_parquet(path, index=False)
    assert mod.read_stock_frame(path)["turnover"].tolist() == [1e8, 2e8]


def test_parse_windows_rejects_nonsense():
    import argparse
    assert mod.parse_windows("3, 7, 30") == [3, 7, 30]
    assert mod.parse_windows("5,5") == [5]
    for bad in ("0", "251", "abc", ""):
        with pytest.raises((argparse.ArgumentTypeError, ValueError)):
            mod.parse_windows(bad)


# —— 读接口内核（stock_selector.sector_heat）：只读预计算文件，不做算术 ——
import numpy as np

from stock_selector import sector_heat as reader


def _board_df():
    return pd.DataFrame({
        "kind": ["industry", "concept", "industry"],
        "board_code": ["A05", "BK0425", "J66"],
        "board_name": ["农林牧渔服务", "工程建设", "货币金融服务"],
        "stock_count": [7, 65, 40],
        "ret_pct": [1.5, 3.2, -2.0],
        "member_count": [7, 90, 40],
        "asof": pd.to_datetime(["2026-10-09"] * 3),
        "window": [3, 3, 7],
    })


def _member_df():
    return pd.DataFrame({
        "kind": ["industry", "industry"],
        "board_code": ["A05", "A05"],
        "board_name": ["农林牧渔服务", "农林牧渔服务"],
        "stock_code": ["000001", "600519"],
        "stock_name": ["平安银行", "贵州茅台"],
        "close": [11.59, 1480.0],
        "ret_pct": [np.float64(2.11), np.float64(-1.5)],
        "window": [3, 3],
        "asof": pd.to_datetime(["2026-10-09"] * 2),
    })


@pytest.fixture
def heat_env(tmp_path, monkeypatch):
    heat_dir = tmp_path / "sector_heat"
    heat_dir.mkdir()
    _board_df().to_parquet(heat_dir / "board.parquet", index=False)
    _member_df().to_parquet(heat_dir / "member.parquet", index=False)
    kline_dir = tmp_path / "stock_kline"
    kline_dir.mkdir()
    raw = _raw_kline([10.0, 11.0, 12.0])
    raw["open"], raw["high"], raw["low"], raw["ma5"] = raw["close"], raw["close"] + 0.5, raw["close"] - 0.5, 11.0
    raw.to_parquet(kline_dir / "000001.parquet", index=False)
    monkeypatch.setattr(reader, "HEAT_DIR", heat_dir)
    monkeypatch.setattr(reader, "KLINE_DIR", kline_dir)
    return tmp_path


def test_heat_sorts_desc_and_is_json_safe(heat_env):
    out = reader.heat(3)
    assert out["asof"] == "2026-10-09"
    assert out["windows"] == [3, 7]
    assert [b["board_code"] for b in out["boards"]] == ["BK0425", "A05"]
    assert out["boards"][0]["ret_pct"] == 3.2
    import json
    json.dumps(out)  # numpy 标量/Timestamp 混进来会在这一步炸


def test_heat_kind_filter(heat_env):
    assert [b["board_code"] for b in reader.heat(3, "concept")["boards"]] == ["BK0425"]


def test_board_detail_members_sorted(heat_env):
    out = reader.board_detail("A05", 3)
    assert out["board"]["board_name"] == "农林牧渔服务"
    assert [m["stock_code"] for m in out["members"]] == ["000001", "600519"]
    assert out["member_count"] == 2


def test_board_detail_rejects_bad_code(heat_env):
    with pytest.raises(ValueError):
        reader.board_detail("A05/../etc", 3)


def test_stock_kline_bars_are_plain_types(heat_env):
    out = reader.stock_kline("000001", days=60)
    assert out["code"] == "000001" and out["name"] == "平安银行"
    last = out["bars"][-1]
    assert last["date"] == "2026-09-03" and last["close"] == 12.0
    assert type(last["close"]) is float and type(last["ma5"]) is float


def test_stock_kline_rejects_pathish_code(heat_env):
    with pytest.raises(ValueError):
        reader.stock_kline("../../etc/passwd")


def test_missing_files_raise_readable_error(tmp_path, monkeypatch):
    monkeypatch.setattr(reader, "HEAT_DIR", tmp_path / "nowhere")
    with pytest.raises(reader.HeatDataMissing) as exc:
        reader.heat(3)
    assert "build_sector_heat.py" in str(exc.value)


def test_cache_invalidates_on_file_rewrite(heat_env):
    assert reader.heat(3)["count"] == 2
    df = _board_df()
    df["window"] = 3
    df.to_parquet(heat_env / "sector_heat" / "board.parquet", index=False)
    import os
    path = heat_env / "sector_heat" / "board.parquet"
    os.utime(path, (path.stat().st_atime + 5, path.stat().st_mtime + 5))
    assert reader.heat(3)["count"] == 3
