"""资金榜读接口内核：只把预计算产物按窗口/方向切给前端，进程内不滚动、不换算单位。

为什么单测这些：页面上的每个数字都要能被追问「哪一天、什么口径」。窗口档位在产物里不存在时
必须说清是缺数（薄日）而不是「今天没有资金动向」；单位换算留给前端，服务端只做原样搬运。
"""

import pandas as pd
import pytest

from stock_selector.fundflow import rank as mod


def _write(tmp_path, table=None, meta=None):
    import json
    (tmp_path / "rank").mkdir(parents=True, exist_ok=True)
    if table is not None:
        table.to_parquet(tmp_path / "rank" / "rank.parquet", index=False, compression="zstd")
    (tmp_path / "rank" / "meta.json").write_text(
        json.dumps(meta or {}, ensure_ascii=False), encoding="utf-8")
    mod.RANK_DIR = tmp_path / "rank"
    mod._cache.clear()


def _table():
    return pd.DataFrame([
        {"window": 3, "code": "600000", "name": "浦发银行", "close": 9.0,
         "net_inflow": 3.0e8, "turnover": 2.0e9, "ratio": 0.15, "ret_pct": 4.0, "days": 3},
        {"window": 3, "code": "000001", "name": "平安银行", "close": 11.0,
         "net_inflow": -2.0e8, "turnover": 1.0e9, "ratio": -0.2, "ret_pct": -3.0, "days": 3},
        {"window": 3, "code": "300750", "name": "宁德时代", "close": 200.0,
         "net_inflow": 8.0e8, "turnover": 6.0e9, "ratio": 0.13, "ret_pct": 1.2, "days": 3},
        {"window": 7, "code": "600519", "name": "贵州茅台", "close": 1500.0,
         "net_inflow": 1.0e9, "turnover": 9.0e9, "ratio": 0.11, "ret_pct": 2.0, "days": 7},
    ])


META = {"as_of": "2026-10-29", "windows": [3, 7], "days_used": ["2026-10-27", "2026-10-28", "2026-10-29"],
        "counts_by_window": {"3": 3, "7": 1},
        "thin_days": [{"date": "2026-10-16", "rows": 186}]}


# ---------------------------------------------------------------- 档位与方向

def test_missing_parquet_gives_a_plain_human_message(tmp_path):
    mod.RANK_DIR = tmp_path / "nothing"
    with pytest.raises(mod.FundDataMissing) as exc:
        mod.rank(window=3)
    assert "build_fund_rank.py" in str(exc.value)


def test_side_in_sorts_by_net_inflow_desc(tmp_path):
    _write(tmp_path, _table(), META)
    out = mod.rank(window=3, side="in")
    assert [r["code"] for r in out["rows"]] == ["300750", "600000", "000001"]


def test_side_out_sorts_by_net_inflow_asc(tmp_path):
    _write(tmp_path, _table(), META)
    out = mod.rank(window=3, side="out")
    assert [r["code"] for r in out["rows"]] == ["000001", "600000", "300750"]


def test_limit_truncates_but_total_reports_the_whole_pool(tmp_path):
    """截断只影响返回条数：total 要让前端能说出「全市场 5373 只里取前 50」。"""
    _write(tmp_path, _table(), META)
    out = mod.rank(window=3, side="in", limit=2)
    assert len(out["rows"]) == 2
    assert out["count"] == 2
    assert out["total"] == 3


def test_amounts_pass_through_untouched(tmp_path):
    """单位是元、占比是小数：换算「亿」是前端的事，服务端不能自作主张约整。"""
    _write(tmp_path, _table(), META)
    row = mod.rank(window=3, side="in")["rows"][0]
    assert row["net_inflow"] == 8.0e8
    assert row["ratio"] == pytest.approx(0.13)
    assert row["ret_pct"] == pytest.approx(1.2)


# ---------------------------------------------------------------- 缺数解释

def test_window_absent_from_product_says_why_not_that_it_is_empty(tmp_path):
    """30 日档在产物里没有票 ⇒ notice 必须点名薄日，不能让页面显示成「无资金动向」。"""
    _write(tmp_path, _table(), META)
    out = mod.rank(window=30, side="in")
    assert out["rows"] == []
    assert "30" in out["notice"]
    assert "2026-10-16" in out["notice"]


def test_windows_list_comes_from_the_product_not_a_hardcoded_menu(tmp_path):
    _write(tmp_path, _table(), META)
    assert mod.rank(window=3)["windows"] == [3, 7]


def test_asof_and_days_come_from_meta(tmp_path):
    _write(tmp_path, _table(), META)
    out = mod.rank(window=3)
    assert out["asof"] == "2026-10-29"
    assert out["days"] == 3
    assert out["thin_days"] == [{"date": "2026-10-16", "rows": 186}]


# ---------------------------------------------------------------- 参数把关

def test_illegal_window_is_rejected(tmp_path):
    _write(tmp_path, _table(), META)
    with pytest.raises(ValueError):
        mod.rank(window=5)


def test_illegal_side_is_rejected(tmp_path):
    _write(tmp_path, _table(), META)
    with pytest.raises(ValueError):
        mod.rank(window=3, side="both")


def test_rereads_after_the_script_overwrites_the_file(tmp_path):
    """脚本整文件覆盖后不重启进程也要看到新数据（按 mtime 键控缓存）。"""
    _write(tmp_path, _table(), META)
    assert mod.rank(window=3, side="in")["total"] == 3
    refreshed = _table()
    refreshed.loc[refreshed["code"] == "000001", "net_inflow"] = 9.9e9
    _write(tmp_path, refreshed, META)
    out = mod.rank(window=3, side="in")
    assert out["rows"][0]["code"] == "000001"
    assert out["asof"] == "2026-10-29"
