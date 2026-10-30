"""资金榜预计算的核心口径：3/7/30 交易日滚动求和、窗口内缺日不进榜、占比分母用同期成交额。

为什么单测这些：页面上每个数字都会被当成「主力在买/在卖」的证据。窗口切错（按自然日算）、
停牌票按 0 计入、占比拿单日成交额当分母——这三种错法都会让榜安静地错，不会报错。
"""

import importlib.util
import sys

import pandas as pd

SCRIPT = "scripts/build_fund_rank.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("build_fund_rank", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["build_fund_rank"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load_script()

WINDOWS = (3, 7, 30)


def _days(n, start="2026-09-01"):
    """n 个连续工作日（资金流的 trade_date 就是交易日，不含周末）。"""
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start, periods=n)]


def _rows(dates, code, name, nets, amounts=None, closes=None, **kw):
    amounts = amounts or [1e8] * len(dates)
    closes = closes or [10.0] * len(dates)
    return pd.DataFrame({
        "trade_date": dates, "code": [code] * len(dates), "name": [name] * len(dates),
        "close": closes,
        "pct_change": [0.0] + [(c / p - 1) * 100 for c, p in zip(closes[1:], closes[:-1])],
        "amount": amounts, "main_net_inflow": nets, "valid": True, **kw,
    })


def _frame(*blocks):
    return pd.concat(blocks, ignore_index=True)


# ---------------------------------------------------------------- 窗口与求和

def test_window_is_trading_days_and_sums_only_the_last_n():
    """3 日窗口只加最后 3 个交易日的主力净额，前面那些天一分都不能掺进来。"""
    d = _days(7)
    f = _frame(_rows(d, "600519", "贵州茅台", [1e7, 2e7, 3e7, 4e7, 5e7, 6e7, 7e7]))
    out = mod.build_table(f, windows=WINDOWS[:1])
    row = out[out["window"] == 3].iloc[0]
    assert row["net_inflow"] == (5e7 + 6e7 + 7e7)
    assert row["days"] == 3


def test_ratio_is_window_net_over_window_turnover():
    """占比 = 窗口净额 ÷ 窗口成交额合计（不是单日占比的平均，也不是全期成交额）。"""
    d = _days(3)
    f = _frame(_rows(d, "000001", "平安银行", [1e7, -2e7, 3e7], amounts=[2e8, 2e8, 2e8]))
    row = mod.build_table(f, windows=(3,))
    assert row["net_inflow"].iloc[0] == 2e7
    assert abs(row["ratio"].iloc[0] - 2e7 / 6e8) < 1e-12


def test_ret_pct_compounds_daily_pct_not_raw_close_ratio():
    """区间涨幅用日涨跌幅复利累乘。日线是裸价（§49），直接首末收盘相除会被除权日打穿。"""
    d = _days(4)
    closes = [10.0, 11.0, 11.0, 5.0]          # 第 4 天「跌一半」实为 10 送 10 的假柱
    f = _frame(_rows(d, "600000", "浦发银行", [1e6] * 4, closes=closes))
    f.loc[f.index[-1], "pct_change"] = 0.0    # 除权当日真实涨跌幅 0%
    row = mod.build_table(f, windows=(3,))
    # 窗口内 3 天：+10%（11/11-1=0）与 0% → 复利 = 1.1*1.0*1.0 - 1 = 10%
    assert abs(row["ret_pct"].iloc[0] - 10.0) < 1e-6


# ---------------------------------------------------------------- 缺日与质量

def test_stock_missing_a_day_inside_window_is_dropped():
    """窗口内少一天就是缺数，不能按 0 计入——那会把「没采到」显示成「没资金动向」。

    交易日必须由外部传进来（`build()` 用分区文件名当日历）。只从数据里数日期，
    缺的那一天会让日历自己跟着缩短，票就永远"完整"。
    """
    d = _days(5)
    f = _frame(_rows(d[:3] + d[4:], "300750", "宁德时代", [1e7] * 4))   # 缺 d[3]
    out = mod.build_table(f, windows=(3,), calendar=d)
    assert out.empty or "300750" not in out["code"].tolist()
    # 同一个票换成按数据数出来的日历，就会假进榜——正是上面那条要防的错法
    assert "300750" in mod.build_table(f, windows=(3,), calendar=d[:3] + d[4:])["code"].tolist()


def test_calendar_shorter_than_window_yields_no_rows():
    """日历只有 4 天时不产 30 日榜，也不产 7 日榜。"""
    d = _days(4)
    f = _frame(_rows(d, "600000", "浦发银行", [1e7] * 4))
    out = mod.build_table(f, windows=(7, 30), calendar=d)
    assert out.empty


def test_invalid_row_inside_window_drops_the_stock():
    """valid=False（停牌/无成交额/ST）占了一天，等于窗口不完整，同样剔掉。"""
    d = _days(3)
    f = _frame(_rows(d, "000004", "ST 股", [1e7, 1e7, 1e7]))
    f.loc[f.index[-1], "valid"] = False
    out = mod.build_table(f, windows=(3,))
    assert "000004" not in out["code"].tolist()


def test_stock_with_history_shorter_than_window_is_dropped():
    """上市不足 30 个交易日的票进不了 30 日榜，但能进 3 日榜。"""
    d = _days(5)
    f = _frame(_rows(d, "301999", "新股", [1e7] * 5))
    assert "301999" in mod.build_table(f, windows=(3,))["code"].tolist()
    assert "301999" not in mod.build_table(f, windows=(7,))["code"].tolist()


# ---------------------------------------------------------------- 落盘层


def _write_partitions(daily_dir, dates, rows_by_date):
    daily_dir.mkdir(parents=True, exist_ok=True)
    for day in dates:
        block = rows_by_date[day]
        block["trade_date"] = day
        block["code"] = block["code"].astype(str)
        block.to_parquet(daily_dir / f"{day}.parquet", index=False)


def _block(dates, codes_nets):
    """一天多只票的分区内容。codes_nets: [(code, net_inflow), ...]"""
    return pd.concat([_rows(dates, c, f"票{c}", [n]) for c, n in codes_nets], ignore_index=True)


def test_build_uses_partition_file_names_as_the_calendar(tmp_path):
    """真实分区里少一只票 ⇒ 那只票掉出榜，别的票照算（日历来自文件名，不是行里数出来的）。"""
    daily = tmp_path / "daily"
    d = _days(3)
    both = [("600000", 1e7), ("000001", 2e7)]
    _write_partitions(daily, d, {
        d[0]: _block([d[0]], both),
        d[1]: _block([d[1]], both),
        d[2]: _block([d[2]], both[:1]),   # 第三天没采到 000001
    })

    result = mod.build((3,), daily)
    table = result["table"]
    assert table["code"].tolist() == ["600000"]
    assert table["net_inflow"].iloc[0] == 3e7
    meta = result["meta"]
    assert meta["as_of"] == d[-1]
    assert meta["days_used"] == d
    assert meta["counts_by_window"] == {3: 1}


def test_build_marks_thin_partition_days(tmp_path):
    """只采到 1 行的那天要在 meta 里点名，不然只会看到榜变短而不知道原因。"""
    daily = tmp_path / "daily"
    d = _days(3)
    many = [(f"60000{i}", 1e7) for i in range(5)]
    _write_partitions(daily, d, {
        d[0]: _block([d[0]], many),
        d[1]: _block([d[1]], many),
        d[2]: _block([d[2]], many[:1]),
    })

    meta = mod.build((3,), daily)["meta"]
    assert meta["partition_rows"] == {d[0]: 5, d[1]: 5, d[2]: 1}
    assert [x["date"] for x in meta["thin_days"]] == [d[2]]


# ---------------------------------------------------------------- 表形状

def test_one_row_per_stock_per_window_with_join_keys():
    d = _days(30)
    f = _frame(_rows(d, "600000", "浦发银行", [1e7] * 30),
               _rows(d, "000001", "平安银行", [-2e7] * 30))
    out = mod.build_table(f, windows=WINDOWS)
    assert len(out) == 3 * 2
    assert sorted(out["window"].unique().tolist()) == list(WINDOWS)
    for c in ("window", "code", "name", "close", "net_inflow", "turnover", "ratio", "ret_pct", "days"):
        assert c in out.columns, f"产物缺列 {c}"


def test_newer_partition_replaces_older_day_for_same_code():
    """同一票同一天出现两行（重复采集）时按最后一次采集为准，求和不能翻倍。"""
    d = _days(3)
    f = _frame(_rows(d, "600000", "浦发银行", [1e7, 1e7, 1e7]),
               _rows(d, "600000", "浦发银行", [1e7, 1e7, 5e7]))
    row = mod.build_table(f, windows=(3,))
    assert row["net_inflow"].iloc[0] == 7e7
