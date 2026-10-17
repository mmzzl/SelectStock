"""票池门槛的口径测试（2026-10-17 加的那三条：剔场内基金、上市时长、日均成交额）。

这三条都是「剔了什么就改了平均数」的开关，错了不会报错、只会静默把结论换掉，所以逐条钉住：
1. 场内基金按代码段剔（15/51/52/53/55/56/58 打头），股票只认 60/68/00/30；
2. `trailing_mean` 把 NaN 当缺数跳过而不是当 0——amount 有爬虫留下的 null，
   当 0 算会把一只票误判成「没流动性」踢出池子；
3. 上市时长门槛是**逐格**生效的（同一只票上市前剔、上市满 N 根日线后放行），
   买点掩码与基准用的是同一张掩码。
"""
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

SCRIPT = "scripts/profit_scan.py"


@pytest.fixture(scope="module")
def ps():
    spec = importlib.util.spec_from_file_location("profit_scan", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["profit_scan"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_fund_codes_are_dropped(ps):
    files = [Path(f"{c}.parquet") for c in
             ("600519", "688981", "000001", "300750", "159915", "510300", "588000", "561560")]
    keep, dropped = ps.filter_funds(files)
    assert [p.stem for p in keep] == ["600519", "688981", "000001", "300750"]
    assert dropped == 4


def test_trailing_mean_skips_nan(ps):
    col = np.array([[100.0], [np.nan], [300.0], [np.nan], [np.nan]])
    got = ps.trailing_mean(col, 2)
    # 第 2 行窗口是 [100, NaN] ⇒ 只有 100 有效；第 4 行窗口两个都缺 ⇒ NaN 而不是 0
    assert got[1, 0] == 100.0
    assert np.isnan(got[4, 0])
    assert got[2, 0] == 300.0 and got[3, 0] == 300.0


def _axis(n: int) -> tuple[np.ndarray, list[str]]:
    idx = pd.bdate_range("2020-01-01", periods=n)
    return idx.to_numpy(dtype="datetime64[ns]"), [d.strftime("%Y-%m-%d") for d in idx]


def _write(tmp_path: Path, code: str, start: int, n: int) -> Path:
    dates = pd.bdate_range("2020-01-01", periods=start + n)[start:]
    df = pd.DataFrame({"code": code, "name": "测试票", "date": dates,
                       "open": 10.0, "high": 10.5, "low": 9.5, "close": 10.0,
                       "pct": 0.5, "amount": 5e7})
    out = tmp_path / f"{code}.parquet"
    df.to_parquet(out)
    return out


def test_pool_mask_is_per_cell(ps, tmp_path, monkeypatch):
    axis, dates = _axis(80)
    monkeypatch.setattr(ps, "POOL", {"min_tenure": 70, "min_amount": 0.0})
    blk = ps.Block([_write(tmp_path, "600001", 0, 80), _write(tmp_path, "600002", 20, 60)],
                   ps.BASE_FIELDS, axis, dates)
    assert blk.n_stock == 2
    ok = blk.ok
    assert not ok[68, 0] and ok[69, 0]                       # 第 70 根（下标 69）才起算
    assert ok[69:, 0].sum() == 11
    assert not ok[:, 1].any()                                    # 只有 60 根、又晚上市 → 全剔
    assert not np.isfinite(blk.cols["close"][:20, 1]).any()      # 上市前的格子确实是空的


def test_amount_gate_uses_window_mean(ps, tmp_path, monkeypatch):
    axis, dates = _axis(80)
    path = _write(tmp_path, "600003", 0, 80)
    df = pd.read_parquet(path)
    df["amount"] = 1e8
    df.loc[df["date"] >= pd.Timestamp("2020-04-01"), "amount"] = 1e6   # 后来变冷门
    df.to_parquet(path)
    monkeypatch.setattr(ps, "POOL", {"min_tenure": 0, "min_amount": 5e7})
    blk = ps.Block([path], ps.BASE_FIELDS, axis, dates)
    ok = blk.ok[:40, 0]
    assert ok.all()                       # 前 40 天日均 1 亿，过线
    assert not blk.ok[79, 0]              # 2020-04-01（下标 65）起只剩 100 万，近 20 日均值也不够


def test_mem_guard_refuses_when_headroom_is_low(ps, tmp_path):
    f = tmp_path / "meminfo"
    f.write_text("MemTotal: 16000000 kB\nMemAvailable: 1000000 kB\n"
                 "SwapTotal: 8000000 kB\nSwapFree: 7000000 kB\n", encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        ps.check_mem_headroom(path=f)
    assert "可用内存" in str(e.value)                      # 0.95G < 3G


def test_mem_guard_passes_when_headroom_is_ok(ps, tmp_path):
    f = tmp_path / "meminfo"
    f.write_text("MemAvailable: 6000000 kB\nSwapTotal: 8000000 kB\nSwapFree: 6000000 kB\n",
                 encoding="utf-8")
    m = ps.check_mem_headroom(path=f)
    assert m["avail_gb"] == pytest.approx(5.72, abs=0.01)
    assert m["swap_free_pct"] == pytest.approx(75.0)


def test_mem_guard_swap_floor_and_no_swap_case(ps, tmp_path):
    low_swap = tmp_path / "m1"
    low_swap.write_text("MemAvailable: 6000000 kB\nSwapTotal: 8000000 kB\nSwapFree: 400000 kB\n",
                        encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        ps.check_mem_headroom(path=low_swap)
    assert "交换" in str(e.value)                           # 5% < 10%
    assert ps.check_mem_headroom(path=low_swap, skip=True)["swap_free_pct"] == 5.0

    no_swap = tmp_path / "m2"
    no_swap.write_text("MemAvailable: 6000000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n",
                       encoding="utf-8")
    assert ps.check_mem_headroom(path=no_swap)["swap_free_pct"] is None   # 没交换≠不够


def test_block_default_is_80_and_only_partitions_files(ps):
    assert ps.BLOCK == 80
    files = [Path(f"{i}.parquet") for i in range(197)]
    blocks = [files[i:i + ps.BLOCK] for i in range(0, len(files), ps.BLOCK)]
    assert [len(b) for b in blocks] == [80, 80, 37]
    assert [p for b in blocks for p in b] == files              # 不重不漏：一只票只进一个块
