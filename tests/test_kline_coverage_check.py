"""K线覆盖率体检（B，docs §48.3）：每票每月覆盖率 + 全场单日票数，两个尺子各抓一种洞。

写错就会静默漏报或天天误报，所以四件纯函数先测：
月份窗口的边界（`date` 是 "YYYY-MM-DD 15:00" 字符串，比较走字典序）、
覆盖率的口径（分母是**这个月真实的交易日数**，不是 21/22 拍脑袋）、
「上月活跃」闸门（新股当月上市、当月停牌不许报成洞）、
薄日判定（一天少了两成票＝采集只爬了一半，跟「整票没数据」是两种病）。
"""
import importlib.util

SCRIPT = "scripts/kline_coverage_check.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("kline_coverage_check", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_month_window_is_lexicographic_on_the_date_string():
    mod = _load_script()
    assert mod.month_window("2026-10-29", 3) == ("2026-08-01", "2026-11-01")
    assert mod.month_window("2026-01-15", 4) == ("2025-10-01", "2026-02-01")


def test_month_of_extracts_yyyy_mm():
    mod = _load_script()
    assert mod.month_of("2026-10-29 15:00") == "2026-10"
    assert mod.month_of("2026-10-29") == "2026-10"


def test_holes_use_real_trading_days_and_require_previous_month_activity():
    mod = _load_script()
    monthly = {  # (code, month) → 有票的天数
        ("600519", "2026-08"): 21, ("600519", "2026-09"): 2, ("600519", "2026-10"): 18,
        ("301999", "2026-09"): 1, ("301999", "2026-10"): 5,     # 上月不活跃＝新股上市，不算洞
        ("000001", "2026-09"): 20, ("000001", "2026-10"): 20,
    }
    need = {"2026-08": 21, "2026-09": 20, "2026-10": 20}
    got = mod.holes(monthly, need, threshold=0.9)
    assert [(h["code"], h["month"]) for h in got] == [("600519", "2026-09")]
    assert got[0]["ratio"] == 0.1
    assert got[0]["need"] == 20 and got[0]["have"] == 2


def test_thin_days_flag_partial_crawl_not_market_holidays():
    mod = _load_script()
    counts = {"2026-10-26": 5000, "2026-10-27": 4990, "2026-10-28": 1200, "2026-10-29": 4980}
    thin = mod.thin_days(counts, ratio=0.9)
    assert [d["date"] for d in thin] == ["2026-10-28"]
    assert thin[0]["codes"] == 1200 and thin[0]["full"] == 5000


def test_thin_days_empty_when_every_day_full():
    mod = _load_script()
    assert mod.thin_days({"2026-10-26": 4800, "2026-10-27": 4810}, ratio=0.9) == []


def test_today_is_dropped_until_the_crawl_finishes():
    """当天爬虫还在写（队尾能漂到 17:05 之后），不剔掉就天天误报「薄日」。"""
    mod = _load_script()
    counts = {"2026-10-27": 6901, "2026-10-28": 6903, "2026-10-29": 5224}
    got = mod.complete_days(counts, "2026-10-29")
    assert "2026-10-29" not in got and len(got) == 2
    # 今天没有一根票（还没开始写）时，昨天照样是最后一天，行为不变
    assert mod.complete_days({"2026-10-28": 6903}, "2026-10-29") == {"2026-10-28": 6903}


def test_alert_text_carries_both_kinds_and_the_caliber():
    mod = _load_script()
    holes = [{"code": "600519", "month": "2026-09", "have": 2, "need": 20, "ratio": 0.1}]
    thin = [{"date": "2026-10-28", "codes": 1200, "full": 5000}]
    title, text = mod.alert_text(holes, [], [], thin,
                                months=["2026-08", "2026-09", "2026-10"])
    assert "K线覆盖" in title
    assert "600519" in text and "2026-09" in text and "2/20" in text
    assert "2026-10-28" in text and "1200" in text
    assert "并集" in text      # 口径必须随消息一起出去：两批 f=9/f=0 是并起来算的


def test_split_holes_separates_our_gap_from_a_real_suspension():
    """上游自己那段时间就没行情＝停牌/退市，不是我们的洞；报成洞就是天天误报，等于没人看。

    实测：2026-10-29 那一轮 35 个洞里最差的 5 个，拿去问上游（新浪日线，爬虫的备源）——
    002717 上游也只到 10-12、512390 到 09-04、688496 到 09-10、301139 到 10-27，
    也就是市场本来就没那些天的票；只有 002388 上游天天有。所以判据是「上游在那个月有几天」，
    不是「上游最后一次有行情是哪天」——301139 九月停牌、十月复牌，用后者会被误判成我们的洞。
    """
    mod = _load_script()
    found = [{"code": "002717", "month": "2026-10", "have": 3, "need": 15, "ratio": 0.2},
             {"code": "600001", "month": "2026-10", "have": 4, "need": 15, "ratio": 0.27},
             {"code": "000999", "month": "2026-10", "have": 5, "need": 15, "ratio": 0.33},
             {"code": "301139", "month": "2026-09", "have": 1, "need": 21, "ratio": 0.05}]
    counts = {("002717", "2026-10"): 6, ("600001", "2026-10"): 15,
              ("000999", "2026-10"): None, ("301139", "2026-09"): 2}
    real, halted, unknown = mod.split_holes(found, counts, threshold=0.9)
    assert [h["code"] for h in real] == ["600001"]
    assert sorted(h["code"] for h in halted) == ["002717", "301139"]
    assert [h["code"] for h in unknown] == ["000999"]


def test_a_code_can_be_halted_one_month_and_our_gap_the_next():
    """判据必须按 (票, 月) 走：一票九月停牌、十月复牌却爬漏了，按票来问就会整票放过。"""
    mod = _load_script()
    found = [{"code": "301139", "month": "2026-09", "have": 1, "need": 21, "ratio": 0.05},
             {"code": "301139", "month": "2026-10", "have": 2, "need": 15, "ratio": 0.13}]
    counts = {("301139", "2026-09"): 1, ("301139", "2026-10"): 15}
    real, halted, unknown = mod.split_holes(found, counts, threshold=0.9)
    assert [(h["code"], h["month"]) for h in real] == [("301139", "2026-10")]
    assert [(h["code"], h["month"]) for h in halted] == [("301139", "2026-09")]
    assert unknown == []


def test_upstream_days_counts_backup_source_bars_in_the_month_only():
    """备源给的是「最近 N 根」，所以按窗口截日期；取不到要回 None（不能当成 0 天＝停牌）。"""
    mod = _load_script()
    bars = [{"day": "2026-09-30"}, {"day": "2026-10-08 15:00"}, {"day": "2026-10-09"},
            {"day": "2026-10-12"}, {"day": "2026-11-03"}]
    assert mod.upstream_days("002717", "2026-10-01", "2026-11-01",
                             fetch=lambda code: bars) == 3
    assert mod.upstream_days("000001", "2026-10-01", "2026-11-01", fetch=lambda code: []) == 0

    def boom(code):
        raise RuntimeError("throttled")

    assert mod.upstream_days("000001", "2026-10-01", "2026-11-01", fetch=boom) is None


def test_verify_counts_asks_upstream_once_per_hole_month():
    """每个「票·月」问一次上游，窗口按那个月的边界给；同一票跨月要分开问（月份不同）。"""
    mod = _load_script()
    found = [{"code": "002717", "month": "2026-10", "have": 3, "need": 15, "ratio": 0.2},
             {"code": "301139", "month": "2026-09", "have": 1, "need": 21, "ratio": 0.05},
             {"code": "002717", "month": "2026-09", "have": 4, "need": 21, "ratio": 0.19}]
    asked = []

    def fake_ask(code, beg, end):
        asked.append((code, beg, end))
        return None if code == "301139" else 6

    counts = mod.verify_counts(found, fake_ask)
    assert counts == {("002717", "2026-10"): 6, ("301139", "2026-09"): None,
                      ("002717", "2026-09"): 6}
    assert ("002717", "2026-10-01", "2026-11-01") in asked
    assert ("301139", "2026-09-01", "2026-10-01") in asked
    assert len(asked) == 3          # 三个票·月各问一次，没有重复请求


def test_alert_text_splits_the_two_and_only_alarms_on_real_holes():
    mod = _load_script()
    real = [{"code": "600001", "month": "2026-10", "have": 4, "need": 15, "ratio": 0.27}]
    halted = [{"code": "002717", "month": "2026-10", "have": 3, "need": 15, "ratio": 0.2}]
    title, text = mod.alert_text(real, halted, [], [],
                                months=["2026-08", "2026-09", "2026-10"])
    assert "600001" in text and "真缺数" in text
    assert "002717" in text and "停牌" in text
    assert "上游同样没那段行情" in text      # 为什么它不算我们的洞，要说清楚
