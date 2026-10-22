"""qlib_daily_universe 的五条筛，重点是「ST 判据必须用现名」和「流动性判不了就当不可成交」。

背景（2026-10-18 实测）：本仓两份名字表都过期了 5 个月（data/csv/all_stock.csv 落盘 05-24、
Mongo bk_stocks 最近 imported_at 05-19），拿它们判 ST 等于没判——全市场名单里因此躺着 113 只
现名是 ST/*ST/退 的票（含当日榜内第 16 名 *ST卓然、第 28 名 ST龙大）。
这些用例把「现名为准」和「名字取不到就不进名单」钉住，免得以后又静默退回旧表。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import qlib_daily_universe as uni  # noqa: E402

STALE = """code,code_name
sh.600001,老代码A
sh.600002,旧表标ST
sh.000022,沪公司债
sz.000022,深振业Ａ
sz.300003,旧表正常
"""

LIQ_OK = 2e8      # 「过得了流动性筛」的样本值（下限是 1e8，见 uni.MIN_AMOUNT）
CAL_DAYS = 40     # 测试面板日历的天数（要 > AMT_WINDOW）
DAYS = [f"2026-01-{i + 1:02d}" for i in range(CAL_DAYS)]


def _bin(cn: Path, sym: str, start: int, closes: list[float],
         amounts: list[float | None]) -> None:
    """往 bin 面板里写一只票的 close/amount 两列：首格是起始日历下标，后面一天一格。

    None 写 NaN，就是面板里「这天没行情」（停牌、或 bin 还没长到那天）。
    """
    import numpy as np

    d = cn / "features" / sym.lower()
    d.mkdir(parents=True, exist_ok=True)
    for field, values in (("close", closes), ("amount", amounts)):
        arr = np.hstack([[start], np.array(
            [np.nan if v is None else v for v in values], dtype="<f")])
        arr.astype("<f").tofile(str(d / f"{field}.day.bin"))


def _write_panel(root: Path, symbols: list[str], bars: int = 20,
                 amount=LIQ_OK) -> Path:
    """写一个 bin 面板（calendars/day.txt + features/<小写符号>/<字段>.day.bin）。

    `bars` 根日线一律贴在**日历尾部**（起始下标 = CAL_DAYS - bars），这就是打分当天能读到
    的最近一段。amount 可以是单个数值，也可以是 {符号: 数值或 None}，字典里没列到的取
    LIQ_OK；None = 面板里那几天有价无额（东财断供那几天的形状）。
    **bars 默认 20**：有效天数低于 `AMT_MIN_DAYS`(10) 会被判成「判不了流动性」，
    那和「不流动」是两条不同的筛，测试里不能混着用。
    """
    cal = root / "calendars"
    cal.mkdir(parents=True, exist_ok=True)
    (cal / "day.txt").write_text("\n".join(DAYS) + "\n", encoding="utf-8")
    start = CAL_DAYS - bars
    for sym in symbols:
        value = amount.get(sym, LIQ_OK) if isinstance(amount, dict) else amount
        _bin(root, sym, start, [1.0] * bars, [None if value is None else float(value)] * bars)
    return root


def _panel(tmp_path: Path, symbols: list[str], **kw) -> Path:
    return _write_panel(tmp_path / "cn", symbols, **kw)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """面板 4 只 + 旧名字表 + 假的现名来源；返回 {符号: 现名} 的可变字典。"""
    panel = _panel(tmp_path, ["SH600001", "SH600002", "SZ000022", "SZ300003"])
    stale = tmp_path / "all_stock.csv"
    stale.write_text(STALE, encoding="utf-8")
    monkeypatch.setattr(uni, "BIN_PANEL", panel)
    monkeypatch.setattr(uni, "NAME_FILE", stale)
    live: dict[str, str] = {
        "600001": "*ST卓然",      # 旧表正常 → 现名 ST：必须剔
        "600002": "老代码B",      # 旧表标 ST → 现名正常：必须放（摘帽）
        "000022": "深振业Ａ",     # 与 sh.000022 撞号的深市代码
    }
    # SZ300003 现名取不到 → 退旧表，旧表里它是「旧表正常」
    from stock_selector.quote import tencent

    monkeypatch.setattr(tencent, "fetch_names",
                        lambda codes, timeout=8.0, retries=2:
                        {c: live[c] for c in codes if c in live})
    return live


def test_st_is_judged_by_the_live_name_not_the_stale_file(sandbox):
    kept, report = uni.build(min_bars=3, keep_st=False, names_mode="live")
    assert "SH600001" not in kept          # 现名 *ST ⇒ 剔
    assert "SH600002" in kept              # 旧表 ST、现名正常 ⇒ 放
    assert report["dropped_st"] == 1
    assert report["names_live"] == 3         # 600001/600002/000022 有现名
    assert report["names_from_stale_file"] == 1  # SZ300003 取不到现名，走兜底


def test_a_code_with_no_name_at_all_does_not_enter(sandbox):
    """两边都取不到名字 = 没法判 ST，只能不进名单并计数，不能当成「不是 ST」放过去。"""
    sandbox.pop("000022")
    stale = uni.NAME_FILE.read_text(encoding="utf-8").replace("sz.000022,深振业Ａ\n", "")
    uni.NAME_FILE.write_text(stale, encoding="utf-8")
    kept, report = uni.build(min_bars=3, keep_st=False, names_mode="live")
    assert "SZ000022" not in kept
    assert report["dropped_no_name"] == 1


def test_stale_names_are_keyed_by_market_and_number(tmp_path):
    """六位数字会跨市场撞车：sh.000022 是「沪公司债」，不能顶给 SZ000022。"""
    path = tmp_path / "all_stock.csv"
    path.write_text(STALE, encoding="utf-8")
    names = uni.read_names(path)
    assert names["SH000022"] == "沪公司债"
    assert names["SZ000022"] == "深振业Ａ"
    assert names["SH000022"] != names["SZ000022"]   # 同数字不同市场，不能互相顶名
    assert "SZ300003" in names and names["SZ300003"] == "旧表正常"


def test_candidates_come_from_the_panel_not_the_name_file(sandbox, tmp_path):
    """面板有日线、旧表没这条记录的新票，只要拿得到现名就该进名单（旧版会静默丢掉）。"""
    _panel(tmp_path, ["SH600001", "SH600002", "SZ000022", "SZ300003", "SZ301999"])
    sandbox["301999"] = "新上市票"
    kept, report = uni.build(min_bars=3, keep_st=False, names_mode="live")
    assert "SZ301999" in kept
    assert report["panel_stocks"] == 5


def test_offline_mode_uses_only_the_stale_file(sandbox, monkeypatch):
    """--names file：完全不碰网络，ST 判据退成旧表口径，报告里要能看出来。"""
    from stock_selector.quote import tencent

    def boom(*a, **kw):
        raise AssertionError("file 模式不该发请求")

    monkeypatch.setattr(tencent, "fetch_names", boom)
    kept, report = uni.build(min_bars=3, keep_st=False, names_mode="file")
    assert "SH600002" not in kept           # 旧表标 ST ⇒ 按旧表剔掉
    assert "SH600001" in kept               # 旧表看不出它已经 ST
    assert report["names_mode"] == "file"
    assert report["names_live"] == 0


def test_thin_liquidity_is_dropped_and_counted(sandbox):
    """20 日均成交额不足下限的票不进名单，且单独计数——放开全市场后这是主要的一道减量。"""
    _write_panel(uni.BIN_PANEL, ["SH600001", "SH600002", "SZ000022", "SZ300003"],
                 amount={"SH600002": 5e7})
    kept, report = uni.build(min_bars=3, keep_st=False, names_mode="live")
    assert "SH600002" not in kept
    assert report["dropped_illiquid"] == 1
    assert report["dropped_no_amount"] == 0


def test_no_usable_amount_is_not_the_same_as_passing(tmp_path, monkeypatch):
    """面板 amount 整列空（东财断供那几天的形状）⇒ 判不了流动性，剔除并单独计数，不能当过筛。

    另一只票只有 5 天有效额（不足 AMT_MIN_DAYS=10）：同一条原则，均值再高也不算。
    """
    panel = _panel(tmp_path, ["SH600001", "SH600002"],
                   bars=20, amount={"SH600001": None})
    stale = tmp_path / "all_stock.csv"
    stale.write_text(STALE, encoding="utf-8")
    monkeypatch.setattr(uni, "BIN_PANEL", panel)
    monkeypatch.setattr(uni, "NAME_FILE", stale)
    from stock_selector.quote import tencent

    monkeypatch.setattr(tencent, "fetch_names",
                        lambda codes, timeout=8.0, retries=2:
                        {"600001": "沪公司正常", "600002": "沪流动票"})
    kept, report = uni.build(min_bars=3, keep_st=False, names_mode="live")
    assert kept == ["SH600002"]
    assert report["dropped_no_amount"] == 1
    assert report["dropped_illiquid"] == 0


def test_min_amount_zero_turns_the_gate_off(sandbox):
    """--min-amount 0 = 关掉这道筛（调试/对照用），此时薄流动票要能放进来。"""
    _write_panel(uni.BIN_PANEL, ["SH600001", "SH600002", "SZ000022", "SZ300003"],
                 amount={"SH600002": 5e7})
    kept, report = uni.build(min_bars=3, keep_st=False, names_mode="live",
                             min_amount=0)
    assert "SH600002" in kept
    assert report["dropped_illiquid"] == 0
    assert report["min_amount"] == 0


# ---------------------------------------------------------------- 面板口径：读 bin

def test_the_liquidity_window_is_the_panel_calendar_tail(sandbox):
    """流动性看的是**面板日历最后 20 格**，不是这只票自己那串数的尾巴。

    这是换成 bin 口径的意义：csv 面板那份拷贝停在 10-16（2026-10-23 实测），于是「最近 20 天」
    事实上是 9/17~10/16，打分读的却是到 10-22 的面板。SH600002 只有日历头 20 天有额、
    最近 20 天全空，必须按「判不了」剔掉，不能拿它自己那串旧数当最近行情放进来。
    """
    cn = uni.BIN_PANEL
    _bin(cn, "SH600002", 0, [1.0] * 20, [LIQ_OK] * 20)          # 额只在旧的那段
    kept, report = uni.build(min_bars=3, keep_st=False, names_mode="live")
    assert "SH600002" not in kept
    assert report["dropped_no_amount"] == 1


def test_candidates_are_the_bin_dirs_that_scoring_can_read(sandbox):
    """候选清单 = 面板 features 下的目录（打分读得到的那些票），不是 csv 拷贝的文件名。

    只有一只票有 bin 目录、csv 面板里根本没这条时，它照样要进候选计数——否则「面板有没有这只」
    和「名单里有没有这只」会各说各话（10-21/22 沪市那次就是这么错开的）。
    """
    cn = uni.BIN_PANEL
    _bin(cn, "SH600009", CAL_DAYS - 20, [1.0] * 20, [LIQ_OK] * 20)
    sandbox["600009"] = "新面板里的票"
    kept, report = uni.build(min_bars=3, keep_st=False, names_mode="live")
    assert "SH600009" in kept
    assert report["panel_stocks"] == 5


def test_the_report_says_which_day_the_panel_ends(sandbox):
    """名单必须自己说清「我是按哪天的面板筛的」：15:55 跑的时候增量链还没写今天，
    不说出边界就没法判断这份名单是筛到 10-22 还是筛到停在 10-16 的旧拷贝。
    """
    kept, report = uni.build(min_bars=3, keep_st=False, names_mode="live")
    assert report["panel_end"] == DAYS[-1]
