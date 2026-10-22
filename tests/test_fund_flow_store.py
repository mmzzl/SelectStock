"""资金流数据管道：日分区存储、唯一去重合并、占比标准化、质量标记、断档自检与告警去重。

这层是任务 #171 阶段 1 的验收面：**攒样本的管道本身**必须可测——「断档当天可见」
「重复写入不翻倍」「T 日数据可审计」这些要求一旦只能靠肉眼盯日志，一年以后没人记得
它们是否真的成立。网络全部注入掉：用例不碰东财。
"""

import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stock_selector.fundflow import store                                   # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "fetch_fund_flow", ROOT / "scripts" / "fetch_fund_flow.py")
fetch = importlib.util.module_from_spec(_SPEC)
sys.modules["fetch_fund_flow"] = fetch
_SPEC.loader.exec_module(fetch)

DAY = "2026-10-16"
STAMP = int(pd.Timestamp("2026-10-16T15:00:00+08:00").timestamp())


def daily_rows(**overrides):
    """两行最小截面：600000 恒正常，000001 那行可用 overrides 改坏。"""
    base = {"date": DAY, "code": "600000", "name": "浦发银行", "close": 8.0,
            "pct_change": 0.5, "amount": 2_000_000.0,
            "main_net_inflow": 100_000.0, "xlarge_net_inflow": 60_000.0,
            "large_net_inflow": 40_000.0, "medium_net_inflow": 20_000.0,
            "small_net_inflow": -120_000.0}
    other = {"date": DAY, "code": "1", "name": "平安银行", "close": 10.0,
             "pct_change": -0.5, "amount": 1_000_000.0,
             "main_net_inflow": -500_000.0, "xlarge_net_inflow": -300_000.0,
             "large_net_inflow": -200_000.0, "medium_net_inflow": -100_000.0,
             "small_net_inflow": 600_000.0}
    other.update(overrides)
    return pd.DataFrame([base, other])


def one_row(date=DAY, code="600000", **overrides):
    row = {"date": date, "code": code, "name": "N", "close": 1.0, "pct_change": 0.0,
           "amount": 1.0}
    row.update({f"{tier}_net_inflow": 0.0 for tier in store.TIERS})
    row.update(overrides)
    return row


@pytest.fixture
def tmp_daily(tmp_path, monkeypatch):
    """把脚本的根目录指到 tmp：run_daily/run_repair 都从模块常量取路径。"""
    monkeypatch.setattr(fetch, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(fetch, "KLINE_DIR", tmp_path / "kline")
    monkeypatch.setattr(fetch, "SNAPSHOT_DIR", tmp_path / "snapshot")
    (tmp_path / "kline").mkdir()
    (tmp_path / "snapshot").mkdir()
    return tmp_path / store.DAILY_SUBDIR


# ---------------------------------------------------------------------------
# 标准化与质量标记
# ---------------------------------------------------------------------------
def test_normalize_computes_ratio_from_amount_and_pads_code():
    frame = store.normalize_and_flag(daily_rows(), data_ts=f"{DAY}T15:00", collected_at="X")
    assert list(frame.columns) == store.DAILY_COLUMNS
    row = frame[frame["code"] == "000001"].iloc[0]
    assert row["main_net_inflow"] == pytest.approx(-500_000.0)
    assert row["main_ratio"] == pytest.approx(-0.5)          # -50万 ÷ 100万
    assert row["small_ratio"] == pytest.approx(0.6)
    assert bool(row["valid"]) and pd.isna(row["exclude_reason"])
    assert row["data_ts"] == f"{DAY}T15:00" and row["collected_at"] == "X"
    assert frame["trade_date"].tolist() == [DAY, DAY]


def test_zero_amount_is_not_filled_with_zero_ratio():
    """成交额为 0（停牌/无成交）时占比留 NaN：「没有分母」和「净流入恰好为零」是两件事。"""
    frame = store.normalize_and_flag(daily_rows(amount=0.0), data_ts="", collected_at="")
    bad = frame[frame["code"] == "000001"].iloc[0]
    assert bad["exclude_reason"] == store.REASON_ZERO_AMOUNT and not bad["valid"]
    assert pd.isna(bad["main_ratio"])
    assert bool(frame[frame["code"] == "600000"].iloc[0]["valid"])


def test_missing_amount_is_a_different_reason_than_zero_amount():
    """成交额未知（缺数）与成交额为 0（事实）必须分开记，否则以后没法判断覆盖率。"""
    frame = store.normalize_and_flag(daily_rows(amount=float("nan")),
                                     data_ts="", collected_at="")
    row = frame[frame["code"] == "000001"].iloc[0]
    assert row["exclude_reason"] == store.REASON_NO_AMOUNT and pd.isna(row["main_ratio"])


def test_interface_placeholder_dash_becomes_nan_and_stays_parquet_writable():
    """接口对「此刻没有值」发的是字符串 '-'（北交所/基金/停牌票的 close 实测如此）。

    2026-10-17 就栽在这里：`-'` 留在 close 列里整列变 object，`to_parquet` 抛
    `Could not convert '-'`，一轮 60 页的全市场采集直接白跑。所以这里既断言转成 NaN，
    也断言真的能落盘——「不抛异常」本身就是这条管线的验收面。
    """
    frame = store.normalize_and_flag(daily_rows(close="-", pct_change="-"),
                                     data_ts="", collected_at="")
    row = frame[frame["code"] == "000001"].iloc[0]
    assert pd.isna(row["close"]) and pd.isna(row["pct_change"])
    assert row["main_ratio"] == pytest.approx(-0.5)          # 有成交额，占比照算


def test_dash_row_written_into_partition(tmp_daily):
    frame = store.normalize_and_flag(daily_rows(close="-"), data_ts="t", collected_at="c")
    written = store.write_partitions(tmp_daily, frame)
    assert written[DAY] == 2
    back = store.read_partition(store.partition_path(tmp_daily, DAY))
    assert pd.isna(back[back["code"] == "000001"].iloc[0]["close"])


def test_st_and_delisting_names_are_flagged_but_kept():
    """剔与不剔是读时选择：行必须留着，只打标记——否则截面票池有多大永远说不清。"""
    for name in ("ST摘帽", "*ST发展", "退市海润"):
        frame = store.normalize_and_flag(daily_rows(name=name), data_ts="", collected_at="")
        row = frame[frame["code"] == "000001"].iloc[0]
        assert row["exclude_reason"] == store.REASON_ABNORMAL_NAME, name
        assert len(frame) == 2


# ---------------------------------------------------------------------------
# 合并语义与幂等
# ---------------------------------------------------------------------------
def test_merge_keeps_old_values_and_prefers_non_null_new():
    existing = store.normalize_and_flag(daily_rows(), data_ts="old", collected_at="old")
    new = store.normalize_and_flag(daily_rows(), data_ts="new", collected_at="new")
    new["main_net_inflow"] = None
    merged = store.merge_partition(existing, new)

    assert len(merged) == 2 and set(merged["code"]) == {"000001", "600000"}
    got = merged[merged["code"] == "600000"].iloc[0]
    # 新值非空才覆盖：data_ts 有值就换掉，净额为空就保留旧的
    assert got["data_ts"] == "new" and got["main_net_inflow"] == pytest.approx(100_000.0)


def test_write_partitions_is_idempotent(tmp_daily):
    frame = store.normalize_and_flag(daily_rows(), data_ts="", collected_at="")
    assert store.write_partitions(tmp_daily, frame) == {DAY: 2}
    assert store.write_partitions(tmp_daily, frame) == {DAY: 2}
    assert store.stored_dates(tmp_daily) == {DAY: 2}
    assert sorted(read_codes(tmp_daily, DAY)) == ["000001", "600000"]


def read_codes(daily_dir, day):
    return store.read_partition(store.partition_path(daily_dir, day))["code"].tolist()


# ---------------------------------------------------------------------------
# 水位、交易日历与断档
# ---------------------------------------------------------------------------
def test_water_line_needs_five_partitions_before_judging():
    """冷启动没有可信水位：返回 0 就意味着「永不跳过」，宁可重抓也不能漏一天。"""
    assert fetch.water_line({f"d{i}": 100 for i in range(4)}) == 0
    known = {f"d{i}": 5000 for i in range(6)}
    assert fetch.water_line(known) == int(5000 * fetch.THIN_RATIO)


def write_kline(kline_dir, code, dates):
    pd.DataFrame({"date": pd.to_datetime(dates)}).to_parquet(
        kline_dir / f"{code}.parquet", index=False)


def test_expected_days_use_intersection_not_union(tmp_path):
    """某只锚票偶然停牌：交集只是少认一天（少查一天，无害）；并集会天天假警报。"""
    kline = tmp_path / "kline"
    kline.mkdir()
    days = ["2026-10-14", "2026-10-15", "2026-10-16"]
    for code in store.ANCHOR_CODES:
        write_kline(kline, code, days)
    write_kline(kline, store.ANCHOR_CODES[0], ["2026-10-14", "2026-10-16"])
    assert store.expected_trading_days(kline, 3) == ["2026-10-14", "2026-10-16"]


def test_gap_report_lists_missing_days_and_keeps_them_all(tmp_daily):
    for index in range(6):
        store.write_partitions(tmp_daily, store.normalize_and_flag(
            pd.DataFrame([one_row(date=f"2026-10-{10 + index}")]),
            data_ts="", collected_at=""))
    expected = [f"2026-10-{day}" for day in range(10, 17)]
    report = store.gap_report(tmp_daily, expected)
    assert report["stored"] == 6
    assert report["missing"] == ["2026-10-16"]
    assert report["thin"] == {}


def test_gap_report_does_not_judge_thin_before_five_partitions(tmp_daily):
    """样本不足 5 个分区时不判薄日：冷启动那只会把正常的第一天报成故障。"""
    store.write_partitions(tmp_daily, store.normalize_and_flag(
        pd.DataFrame([one_row(code="000001"), one_row(code="000002")]),
        data_ts="", collected_at=""))
    report = store.gap_report(tmp_daily, [DAY, "2026-10-15"])
    assert report["thin"] == {} and report["missing"] == ["2026-10-15"]


def test_gap_report_flags_a_half_empty_day(tmp_daily):
    """薄日 = 有文件但行数远低于水位：翻页大面积失败留下的正是这种「看着成功」。"""
    for index in range(6):
        day = f"2026-10-{10 + index}"
        rows = [one_row(date=day, code=f"{i:06d}") for i in range(500 if index == 5 else 1000)]
        store.write_partitions(tmp_daily, store.normalize_and_flag(
            pd.DataFrame(rows), data_ts="", collected_at=""))
    report = store.gap_report(tmp_daily, [f"2026-10-{10 + index}" for index in range(6)])
    assert report["missing"] == []
    assert report["thin"] == {"2026-10-15": 500}


# ---------------------------------------------------------------------------
# 采集脚本：交易日推导、字段映射、幂等跳过
# ---------------------------------------------------------------------------
def test_trading_day_comes_from_the_data_not_the_wall_clock():
    """分区键用 f124 的数据时刻：周末重跑要落回周五，而不是造出一个不存在的周六。"""
    day, moment = fetch.trading_day_of([{"f124": STAMP}], fallback="2026-10-17")
    assert day == DAY and moment.startswith(f"{DAY}T15:00")
    assert fetch.trading_day_of([{"f124": None}], fallback="2026-10-17")[0] == "2026-10-17"


def test_build_daily_frame_maps_fields_and_drops_duplicate_codes():
    rows = [{"f12": "1", "f14": "平安银行", "f2": 10.5, "f3": 1.2, "f6": 1000.0,
             "f62": 100.0, "f66": 60.0, "f72": 40.0, "f78": 20.0, "f84": -120.0},
            {"f12": "1", "f14": "平安银行", "f2": 10.6, "f3": 1.2, "f6": 1000.0,
             "f62": 200.0, "f66": 0.0, "f72": 0.0, "f78": 0.0, "f84": 0.0},
            {"f12": None, "f14": "空行"}]
    frame = fetch.build_daily_frame(rows, trade_date=DAY, data_ts=f"{DAY}T15:00")
    assert len(frame) == 1                                   # 空代码行丢掉、重复代码留一条
    row = frame.iloc[0]
    assert row["code"] == "000001" and row["close"] == pytest.approx(10.6)
    assert row["main_net_inflow"] == pytest.approx(200.0)     # 留的是后一页
    assert row["main_ratio"] == pytest.approx(0.2)


def clist_row(code="600000"):
    return {"f12": code, "f14": "N", "f2": 1.0, "f3": 0.0, "f6": 100.0,
            "f62": 10.0, "f66": 0.0, "f72": 0.0, "f78": 0.0, "f84": 0.0, "f124": STAMP}


def test_run_daily_skips_a_complete_partition(tmp_daily, monkeypatch):
    calls = []

    def fake_page(page, **kwargs):
        calls.append(page)
        return [clist_row()], 5000

    monkeypatch.setattr(fetch, "fetch_daily_page", fake_page)
    dense = pd.DataFrame([one_row(date=f"2026-10-{day}", code=f"{i:06d}")
                          for day in range(11, 17) for i in range(5000)])
    store.write_partitions(tmp_daily, store.normalize_and_flag(dense, data_ts="",
                                                               collected_at=""))

    result = fetch.run_daily()
    assert result["skipped"] and result["trade_date"] == DAY
    assert calls == [1]                                      # 只花一个请求做判断
    assert store.partition_rows(store.partition_path(tmp_daily, DAY)) == 5000


def test_run_daily_fetches_all_pages_and_writes_one_partition(tmp_daily, monkeypatch):
    seen = []

    def fake_page(page, **kwargs):
        seen.append(page)
        return [clist_row(code=f"{page:06d}")], 300          # 3 页 × 100 行

    monkeypatch.setattr(fetch, "fetch_daily_page", fake_page)
    monkeypatch.setattr(fetch, "PAGE_SLEEP", 0)
    result = fetch.run_daily()
    assert seen == [1, 2, 3]
    assert result["ok"] and result["rows"] == 3 and result["valid_rows"] == 3
    assert store.partition_rows(store.partition_path(tmp_daily, DAY)) == 3


def test_run_daily_slows_down_instead_of_hard_retrying(tmp_daily, monkeypatch):
    """连续两页取不到就把页间隔拉长 3 倍：硬重试正是 10-17 把 IP 打进黑名单的成因。"""
    def fake_page(page, **kwargs):
        return None if page in (2, 3) else ([clist_row(code=f"{page:06d}")], 500)

    monkeypatch.setattr(fetch, "fetch_daily_page", fake_page)
    monkeypatch.setattr(fetch, "PAGE_SLEEP", 1.0)
    # 换浏览器身份那条分支排在降速前面，而且会吃掉 streak：本机 data/fund_flow/cookies/
    # 有真 Cookie 时它会成功，用例就变成「看今天有没有 cookie」而不是测降速。这里固定失败。
    monkeypatch.setattr(fetch, "_try_refresh_cookie", lambda: False)
    sleeps: list[float] = []
    result = fetch.run_daily(sleeper=sleeps.append)
    assert result["failed_pages"] == [2, 3]
    # 单页失败不动节奏（0.3 是初始间隔），连续第 2 页失败才降速，之后保持降速
    assert sleeps == [1.0, 3.0, 3.0, 3.0] and result["page_sleep"] == 3.0


def test_run_daily_max_pages_writes_a_partial_day(tmp_daily, monkeypatch):
    """试运行降量（`--max-pages`）会留下部分覆盖的一天，必须在返回值里显式标出来：
    批量接口过后取不到这一天，之后只能靠很慢的逐只补抓。"""
    monkeypatch.setattr(fetch, "fetch_daily_page",
                        lambda page, **kwargs: ([clist_row(code=f"{page:06d}")], 500))
    monkeypatch.setattr(fetch, "PAGE_SLEEP", 0)
    result = fetch.run_daily(max_pages=2, sleeper=lambda *_: None)
    assert result["pages"] == 2 and result["partial"] and result["rows"] == 2
    assert store.partition_rows(store.partition_path(tmp_daily, DAY)) == 2

    monkeypatch.setattr(fetch, "fetch_daily_page",
                        lambda page, **kwargs: ([clist_row(code=f"{page:06d}")], 500))
    full = fetch.run_daily(force=True, sleeper=lambda *_: None)
    assert not full["partial"] and full["pages"] == 5


# ---------------------------------------------------------------------------
# 传输层与第 1 页的耐心重试
# ---------------------------------------------------------------------------

def test_transport_defaults_to_curl_cffi_and_reuses_one_session(monkeypatch):
    """默认通道是 curl_cffi：2026-10-17 同一时间窗内交错 A/B，requests 0/16 全被掐断，
    curl_cffi（Chrome TLS 指纹 + 复用连接）过了 7 次。这里只验「建哪条通道、只建一条」，
    不发真请求——连接复用是关键：旧写法每翻一页重握手一次，等于每页重新过一次节流闸。
    """
    from curl_cffi import requests as cffi_requests

    assert fetch.TRANSPORT == "cffi"
    monkeypatch.setattr(fetch, "_SESSION", None)
    session = fetch.http_session()
    assert isinstance(session, cffi_requests.Session)
    assert fetch.http_session() is session                       # 第二条连接不该被建出来
    assert issubclass(cffi_requests.exceptions.ConnectionError, fetch._CATCH)


def test_run_daily_page1_failure_waits_and_restarts_the_round(tmp_daily, monkeypatch):
    """第 1 页取不到 = 这一天永久缺（批量接口只给当下），所以整轮要等分钟级再重来。
    旧版和翻页共用 0.8/1.6/3.2 秒的梯子，6 秒内就放弃整天，而实测硬窗口是十几~几十分钟。
    """
    asked = []

    def fake_page(page, **kwargs):
        asked.append(page)
        return None

    monkeypatch.setattr(fetch, "fetch_daily_page", fake_page)
    waits: list[float] = []
    result = fetch.run_daily(sleeper=waits.append)
    assert result["ok"] is False and result["reason"] == "page1_failed"
    assert result["rounds"] == fetch.ROUND_RETRIES + 1
    assert asked == [1] * (fetch.ROUND_RETRIES + 1)
    assert waits == list(fetch.ROUND_WAIT_SECONDS)               # 最后一轮失败后不再白等
    assert list(fetch.stored_dates(tmp_daily)) == []


def test_run_daily_page1_recovers_on_a_later_round(tmp_daily, monkeypatch):
    answers = iter([None, ([clist_row()], 100)])
    monkeypatch.setattr(fetch, "fetch_daily_page", lambda page, **kwargs: next(answers))
    waits: list[float] = []
    result = fetch.run_daily(sleeper=waits.append)
    assert result["ok"] and result["rows"] == 1
    assert waits == [fetch.ROUND_WAIT_SECONDS[0]]
    assert store.partition_rows(store.partition_path(tmp_daily, DAY)) == 1


# ---------------------------------------------------------------------------
# 补抓：有界预算 + 轮转游标
# ---------------------------------------------------------------------------
def history_frame(code, dates):
    return pd.DataFrame([{"date": day, "code": code, "name": "N", "close": 1.0,
                          "pct_change": 0.0, "main_net_inflow_pct": 1.0,
                          **{f"{tier}_net_inflow": 1.0 for tier in store.TIERS}}
                         for day in dates])


def test_repair_respects_budget_and_rotates_cursor(tmp_daily, monkeypatch):
    universe = [f"{i:06d}" for i in range(10)]
    asked = []

    def fake_history(code, **kwargs):
        asked.append(code)
        return history_frame(code, ["2026-10-15", DAY])       # 一次请求覆盖多个缺口日

    monkeypatch.setattr(fetch, "fetch_individual_history", fake_history)
    monkeypatch.setattr(fetch, "amount_series", lambda code, days: pd.Series([10.0] * len(days)))

    first = fetch.run_repair([DAY], universe, budget=4, cursor=0, sleeper=lambda *_: None)
    assert first["batch"] == 4 and asked == universe[:4] and first["next_cursor"] == 4
    assert store.partition_rows(store.partition_path(tmp_daily, DAY)) == 4

    second = fetch.run_repair([DAY], universe, budget=4, cursor=first["next_cursor"],
                              sleeper=lambda *_: None)
    assert asked[4:] == universe[4:8]                         # 游标让它接着往下走
    assert store.partition_rows(store.partition_path(tmp_daily, DAY)) == 8
    assert second["written"] == {DAY: 8}


def test_repair_writes_only_target_days(tmp_daily, monkeypatch):
    monkeypatch.setattr(fetch, "fetch_individual_history",
                        lambda code, **kwargs: history_frame(code, ["2026-10-09", DAY]))
    monkeypatch.setattr(fetch, "amount_series", lambda code, days: pd.Series([10.0] * len(days)))
    fetch.run_repair([DAY], ["600000"], budget=1, sleeper=lambda *_: None)
    assert store.stored_dates(tmp_daily) == {DAY: 1}          # 10-09 不在目标日就不写


def test_repair_skips_when_there_is_no_gap(tmp_daily):
    assert fetch.run_repair([], ["600000"], budget=5)["skipped"]
    store.write_partitions(tmp_daily, store.normalize_and_flag(
        daily_rows(), data_ts="", collected_at=""))
    out = fetch.run_repair([DAY], ["000001", "600000"], budget=5, sleeper=lambda *_: None)
    assert out["skipped"] and out["reason"] == "目标日已覆盖全部票池"


def test_repair_reports_failed_stocks(tmp_daily, monkeypatch):
    """网络拿不到就计入清单：告警里要说「哪几只补不上」，不能只说失败。"""
    monkeypatch.setattr(fetch, "fetch_individual_history", lambda code, **kwargs: pd.DataFrame())
    out = fetch.run_repair([DAY], ["000001", "600000"], budget=2, sleeper=lambda *_: None)
    assert out["failed"] == ["000001", "600000"] and out["written"] == {}


def test_repair_stops_at_the_wall_clock_cap(tmp_daily, monkeypatch):
    """2026-10-17 旧版逐只通道跑了 4 小时 22 分，把本机 IP 打进东财黑名单。

    补抓一旦能吃掉整天配额，主通道就再也取不到数——所以超时必须停手，且游标只按
    **实际尝试数**推进：把没跑的票算成跑过，等于让它们永远补不到。
    """
    universe = [f"{i:06d}" for i in range(4)]
    asked: list[str] = []

    def fake_history(code, **kwargs):
        asked.append(code)
        return history_frame(code, [DAY])

    monkeypatch.setattr(fetch, "fetch_individual_history", fake_history)
    monkeypatch.setattr(fetch, "amount_series", lambda code, days: pd.Series([10.0] * len(days)))
    # 注入时钟：第一次取到 0 定出 deadline=120，之后每 60 单位走一格
    ticks = iter([0, 60, 120, 180, 240]).__next__

    out = fetch.run_repair([DAY], universe, budget=4, cursor=0, sleeper=lambda *_: None,
                           clock=ticks, max_seconds=120)
    assert out["planned"] == 4 and out["batch"] == 2          # 计划 4 只，只跑了 2 只
    assert out["stopped_by_time"] and out["next_cursor"] == 2
    assert asked == universe[:2]


# ---------------------------------------------------------------------------
# 自检：漏采补、薄日报
# ---------------------------------------------------------------------------
WEEK = [f"2026-10-{day}" for day in (12, 13, 14, 15, 16, 19, 20)]
POOL = [f"{i:06d}" for i in range(10)]


def fill_days(tmp_daily, monkeypatch):
    """5 个满票池分区 + 1 个只有 2 行的薄日 + 1 个整天没文件的日子。"""
    for day in WEEK[:5]:
        store.write_partitions(tmp_daily, store.normalize_and_flag(
            pd.DataFrame([one_row(date=day, code=code) for code in POOL]),
            data_ts="", collected_at=""))
    store.write_partitions(tmp_daily, store.normalize_and_flag(
        pd.DataFrame([one_row(date=WEEK[5], code=code) for code in POOL[-2:]]),
        data_ts="", collected_at=""))
    for code in store.ANCHOR_CODES:
        write_kline(fetch.KLINE_DIR, code, WEEK)
    monkeypatch.setattr(fetch, "fetch_individual_history",
                        lambda code, **kwargs: history_frame(code, WEEK))
    monkeypatch.setattr(fetch, "amount_series", lambda code, days: pd.Series([10.0] * len(days)))


def test_self_check_repairs_missing_days_only(tmp_daily, monkeypatch):
    """薄日默认**只报不补**：把一天从 2 行补到满票池要几千次逐只请求，等于每天自我 DoS。"""
    asked: list[str] = []
    fill_days(tmp_daily, monkeypatch)
    monkeypatch.setattr(fetch, "fetch_individual_history",
                        lambda code, **kwargs: asked.append(code) or history_frame(code, WEEK))

    report, repair = fetch.run_self_check(7, repair_budget=2, repair_cursor=0,
                                          sleeper=lambda *_: None)
    assert report["missing"] == [WEEK[6]] and report["thin"] == {WEEK[5]: 2}
    assert repair["targets"] == [WEEK[6]] and asked == POOL[:2]
    assert store.partition_rows(store.partition_path(tmp_daily, WEEK[5])) == 2

    report, repair = fetch.run_self_check(7, repair_budget=2, repair_cursor=0,
                                          repair_thin=True, sleeper=lambda *_: None)
    assert repair["targets"] == [WEEK[5], WEEK[6]]
    assert store.partition_rows(store.partition_path(tmp_daily, WEEK[5])) > 2


def test_self_check_counts_rows_not_files_when_reporting(tmp_daily, monkeypatch):
    """水位来自已存分区行数的中位数：告警里要能看见「这天到底有多少行」。"""
    fill_days(tmp_daily, monkeypatch)
    report, _ = fetch.run_self_check(7, repair_budget=0, repair_cursor=0,
                                     sleeper=lambda *_: None)
    assert report["water_line"] == int(10 * fetch.THIN_RATIO)
    assert report["stored"] == 6 and len(report["expected_days"]) == 7
    assert report["thin"] == {WEEK[5]: 2} and report["missing"] == [WEEK[6]]


def test_self_check_says_why_it_skipped_when_only_thin_days(tmp_daily, monkeypatch):
    """只有薄日时不能报「无缺口」：那等于把半截天说成没事，告警就失去了意义。"""
    fill_days(tmp_daily, monkeypatch)
    store.write_partitions(tmp_daily, store.normalize_and_flag(
        pd.DataFrame([one_row(date=WEEK[6], code=code) for code in POOL]),
        data_ts="", collected_at=""))

    report, repair = fetch.run_self_check(7, repair_budget=2, repair_cursor=0,
                                          sleeper=lambda *_: None)
    assert report["missing"] == [] and report["thin"] == {WEEK[5]: 2}
    assert repair["skipped"] and "薄日" in repair["reason"]
    assert "只报不补" in fetch.gap_alert_text(report, repair)[1]


# ---------------------------------------------------------------------------
# 告警：同日去重
# ---------------------------------------------------------------------------
class FakePusher:
    def __init__(self, ok=True):
        self.ok, self.sent, self.DINGTALK = ok, [], "dingtalk"
        self.resolve_webhook = lambda channel, key="": ("ding_ops", "https://x.invalid/h", "")

    def send(self, channel, url, secret, title, text, **kwargs):
        self.sent.append(title)
        return self.ok


def fire_alert(monkeypatch, pusher, report, repair=None, status=None, **kwargs):
    monkeypatch.setitem(sys.modules, "alert_pusher", pusher)
    return fetch.maybe_alert(report, repair or {}, status if status is not None else {},
                             today=DAY, **kwargs)


def gap_report_stub():
    return {"missing": [DAY], "thin": {}, "stored": 0, "expected_days": [DAY, "2026-10-15"]}


def test_alert_fires_once_per_day(tmp_path, monkeypatch):
    pusher = FakePusher()
    status: dict = {}
    assert fire_alert(monkeypatch, pusher, gap_report_stub(), status=status) == "sent"
    assert len(pusher.sent) == 1 and status["alert_date"] == DAY
    assert "缺失 1 个交易日" in pusher.sent[0]
    assert fire_alert(monkeypatch, pusher, gap_report_stub(), status=status) == "deduped"
    assert len(pusher.sent) == 1


def test_failed_send_is_not_recorded(monkeypatch):
    """没发出去就不记日期，否则这条故障在剩下的日子里会永远静默。"""
    status: dict = {}
    assert fire_alert(monkeypatch, FakePusher(ok=False), gap_report_stub(),
                      status=status) == "send_failed"
    assert "alert_date" not in status


def test_alert_stays_silent_when_coverage_is_complete(monkeypatch):
    pusher = FakePusher()
    report = {"missing": [], "thin": {}, "stored": 3, "expected_days": []}
    assert fire_alert(monkeypatch, pusher, report) == "no_gap"
    assert pusher.sent == []


def test_alert_disabled_and_dry_run_never_push(monkeypatch):
    pusher = FakePusher()
    assert fire_alert(monkeypatch, pusher, gap_report_stub(), enabled=False) == "disabled"
    status: dict = {}
    assert fire_alert(monkeypatch, pusher, gap_report_stub(), status=status,
                      dry_run=True) == "dry_run"
    assert pusher.sent == [] and status == {}


def test_alert_lists_the_stocks_that_could_not_be_repaired(monkeypatch):
    pusher = FakePusher()
    text_title, text = fetch.gap_alert_text(gap_report_stub(),
                                            {"batch": 40, "written": {}, "failed": ["000001"]})
    assert "本轮补抓：40 只" in text and "000001" in text
