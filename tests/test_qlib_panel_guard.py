"""面板「缺数据要能自己补、补不上要出声」的三条门槛。

背景（2026-10-22 实测）：爬虫的队尾是沪市（sorted() 后 6 字头排最后），10-21/10-22 两天
沪市行情 16:24~16:31 才落进 Mongo，而面板增量固定在 16:00 跑。qlib 的 `dump_bin dump_update`
对已有股票**只追加比 instrument 结束日更晚的行**，所以那天写进去的 NaN 成了永久空洞：
1,326 只沪市票 10-21/10-22 整天空着，17:30 的打分只剩深市 1,554 只，名单看起来完全正常。

三件事必须钉住：
1. 导出时就按「日 × 市场」统计覆盖率，哪个市场当天缺到不能出名单 → 推钉钉，并把
   几只/该市场总几只一起带上（读数不带口径等于没读数）。
2. 补洞只填 NaN 格子，绝不覆盖已有读数；面板 bin 覆盖不到的日期一律不往后垫
   （qlib 的 bin 是「起始下标 + 连续格子」，垫一格就等于把周一的价格写到周二）。
3. 打分侧自己也要出声：进榜只数明显少于名单 = 半市场的面板，不能只靠上游那条告警。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[1]
for p in (str(REPO), str(REPO / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import qlib_update_bin as ub  # noqa: E402


def _row(code: str, day: str, px: float = 10.0, amount: float = 1e7) -> dict:
    """一条 Mongo 日线（库里 date 带「 15:00」后缀，量纲是「手」；amount=None = 那天有价无额）。"""
    return {"code": code, "date": f"{day} 15:00", "crawl_time": f"{day}T15:30:00",
            "open": px, "high": px * 1.05, "low": px * 0.95, "close": px,
            "volume": 1000, "amount": amount}


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    def sort(self, *a, **kw):
        return self

    def __iter__(self):
        return iter(self._rows)


class FakeCol:
    """只实现 export_window 用到的那半个 API：按 code + date>=since 查。"""

    def __init__(self, rows):
        self.rows = rows

    def find(self, query, projection=None):
        since = query["date"]["$gte"]
        out = [r for r in self.rows
               if r["code"] == query["code"] and str(r["date"])[:10] >= since]
        return _Cursor(sorted(out, key=lambda r: str(r["date"])))


CAL = ["2026-10-20", "2026-10-21", "2026-10-22"]


# ---------------------------------------------------------------- 覆盖率统计

def test_export_window_reports_coverage_by_day_and_market(tmp_path):
    """沪市 10-21 整天没到（迟到），深市三天都有 → 覆盖率里必须是 SH 那天 have=0。"""
    rows = []
    for day in CAL:
        for code in ("000001", "300003"):
            rows.append(_row(code, day))
        for code in ("600001", "600002"):
            if day != "2026-10-21":
                rows.append(_row(code, day))
    stats = ub.export_window(FakeCol(rows), ["600001", "600002", "000001", "300003"],
                             CAL, 0, len(CAL) - 1, tmp_path / "csv")
    cov = stats["coverage"]
    # 2026-10-23 起每格多一列 have_amount（成交额覆盖率），缺行那天它跟着归零，不是另一个数字
    assert cov["2026-10-21"]["SH"] == {"have": 0, "total": 2, "have_amount": 0}
    assert cov["2026-10-21"]["SZ"] == {"have": 2, "total": 2, "have_amount": 2}
    assert cov["2026-10-22"]["SH"] == {"have": 2, "total": 2, "have_amount": 2}


def test_coverage_gate_flags_the_thin_market_with_its_counts():
    cov = {"2026-10-21": {"SH": {"have": 3, "total": 100},
                          "SZ": {"have": 99, "total": 100}}}
    problems = ub.coverage_problems(cov, ["2026-10-21"])
    assert len(problems) == 1
    p = problems[0]
    assert (p["date"], p["market"], p["have"], p["total"]) == \
        ("2026-10-21", "SH", 3, 100)
    assert p["ratio"] == pytest.approx(0.03)


def test_a_thin_market_is_not_judged_when_it_has_only_a_few_names():
    """北交所只有几只票时，一两只停牌就把比例打到 0，报出来只是噪音（阈值见 min_total）。"""
    cov = {"2026-10-21": {"BJ": {"have": 0, "total": 4}}}
    assert ub.coverage_problems(cov, ["2026-10-21"]) == []


def test_the_gate_only_looks_at_the_days_it_was_handed():
    """窗口里更早的日子天生就有停牌/退市缺口，不该天天为历史报错——只看这次要补的那天。"""
    cov = {"2026-10-20": {"SH": {"have": 10, "total": 100}},
           "2026-10-21": {"SH": {"have": 100, "total": 100}}}
    problems = ub.coverage_problems(cov, ["2026-10-21"])
    assert problems == []


def test_alert_text_ships_the_counts_not_just_a_percentage():
    problems = ub.coverage_problems(
        {"2026-10-21": {"SH": {"have": 3, "total": 100}}}, ["2026-10-21"])
    text = ub.coverage_alert_text(problems)
    assert "2026-10-21" in text
    assert "SH" in text and "3" in text and "100" in text   # 分子分母都在
    assert "3.0%" in text                                    # 比例也带上


def test_coverage_counts_the_amount_column_too(tmp_path):
    """「有价无额」是另一种缺：15:45 的快照合并抢在沪市落库前面，那天整市场都没成交额。

    这一列原来不进覆盖率账，所以门是瞎的——OHLC 齐了就报「更新完成」，实际写进去的
    vwap 全是 close 兜底（`dense_frame` 的写法），而那一格之后再也不是 NaN，补不回来。
    """
    rows = []
    for day in CAL:
        for code in ("000001", "000002"):
            rows.append(_row(code, day))
        for code in ("600001", "600002"):
            rows.append(_row(code, day, amount=None if day == "2026-10-22" else 1e7))
    stats = ub.export_window(FakeCol(rows), ["600001", "600002", "000001", "000002"],
                             CAL, 0, len(CAL) - 1, tmp_path / "csv")
    cov = stats["coverage"]
    assert cov["2026-10-22"]["SH"] == {"have": 2, "total": 2, "have_amount": 0}
    assert cov["2026-10-22"]["SZ"] == {"have": 2, "total": 2, "have_amount": 2}
    assert cov["2026-10-21"]["SH"]["have_amount"] == 2


def test_the_gate_also_flags_a_market_that_has_price_but_no_amount():
    """价齐额空必须单独出一条，且要说清是哪一列缺（读的人才会去查快照合并，不是查爬虫）。"""
    cov = {"2026-10-22": {"SH": {"have": 100, "total": 100, "have_amount": 3},
                          "SZ": {"have": 100, "total": 100, "have_amount": 99}}}
    problems = ub.coverage_problems(cov, ["2026-10-22"])
    assert len(problems) == 1
    p = problems[0]
    assert (p["market"], p["field"], p["have"], p["total"]) == ("SH", "amount", 3, 100)
    assert "成交额" in ub.coverage_alert_text(problems)


def test_a_full_price_day_with_a_few_missing_amounts_is_not_a_problem():
    """几只票天生没额（停牌/快照覆盖不到的）不该天天刷屏：只看整市场的比例。"""
    cov = {"2026-10-22": {"SH": {"have": 1300, "total": 1322, "have_amount": 1299}}}
    assert ub.coverage_problems(cov, ["2026-10-22"]) == []


def test_a_day_that_is_missing_bars_is_reported_once_not_twice():
    """整市场没行时，「没日线」一条就够了；再报一条「没成交额」只是同一件事的下游。"""
    cov = {"2026-10-22": {"SH": {"have": 0, "total": 100, "have_amount": 0}}}
    problems = ub.coverage_problems(cov, ["2026-10-22"])
    assert len(problems) == 1
    assert problems[0]["field"] == "close"


# ---------------------------------------------------------------- 补洞

def _make_panel(root: Path, start: int, close_values: list[float]) -> Path:
    """写一个最小面板：calendars/day.txt + SH600001 的 close.day.bin（首格是起始下标）。"""
    (root / "calendars").mkdir(parents=True, exist_ok=True)
    days = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"]
    (root / "calendars" / "day.txt").write_text("\n".join(days) + "\n", encoding="utf-8")
    feats = root / "features" / "sh600001"
    feats.mkdir(parents=True, exist_ok=True)
    arr = np.hstack([[start], np.array(close_values, dtype="<f")])
    arr.astype("<f").tofile(str(feats / "close.day.bin"))
    return root


def _read_bin(path: Path) -> np.ndarray:
    return np.fromfile(path, dtype="<f")


def _write_csv(out_dir: Path, sym: str, days: list[str], closes: list[float]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{sym}.csv").write_text(
        "date,open,high,low,close,volume,amount,vwap,factor\n"
        + "".join(f"{d},1,1,1,{'' if np.isnan(c) else c},1,1,1,1\n" for d, c in zip(days, closes)),
        encoding="utf-8")


def _write_days(out_dir: Path, sym: str, days: list[str], amount: list, vwap: list) -> None:
    """只关心 amount/vwap 两列的窗口 CSV（None/NaN 写成空串 = 库里那天没额）。"""
    out_dir.mkdir(parents=True, exist_ok=True)

    def cell(v):
        return "" if v is None or (isinstance(v, float) and np.isnan(v)) else str(v)

    (out_dir / f"{sym}.csv").write_text(
        "date,open,high,low,close,volume,amount,vwap,factor\n"
        + "".join(f"{d},1,1,1,1,1,{cell(a)},{cell(w)},1\n"
                  for d, a, w in zip(days, amount, vwap)),
        encoding="utf-8")


def _panel_field(panel: Path, field: str, start: int, values: list[float]) -> Path:
    feats = panel / "features" / "sh600001"
    feats.mkdir(parents=True, exist_ok=True)
    arr = np.hstack([[start], np.array(values, dtype="<f")])
    arr.astype("<f").tofile(str(feats / f"{field}.day.bin"))
    return feats / f"{field}.day.bin"


CAL5 = ["2026-01-0" + str(i + 1) for i in range(5)]


def test_fill_holes_only_touches_nan_cells(tmp_path):
    """NaN 格子补上，已有读数一个字都不改（补洞不能变成第二次覆盖）。"""
    panel = _make_panel(tmp_path / "cn", start=1, close_values=[1.0, np.nan, 3.0, np.nan])
    csv_dir = tmp_path / "csv"
    _write_csv(csv_dir, "SH600001", CAL5[1:4], [999.0, 2.5, 999.0])
    report = ub.fill_holes(panel, csv_dir, CAL5, ["close"])
    got = _read_bin(panel / "features" / "sh600001" / "close.day.bin")
    assert list(got[1:3]) == [1.0, 2.5]               # 第 1 格没被 999 覆盖，第 2 格补上了
    assert got[3] == 3.0 and np.isnan(got[4])          # 第 3 格没动，第 4 格没数据可补
    assert int(got[0]) == 1                          # 起始下标不能动
    assert report["filled"] == 1
    assert report["by_date"] == {"2026-01-03": 1}


def test_fill_holes_never_pads_past_the_end_of_the_bin(tmp_path):
    """bin 覆盖不到的日期：一格都不能往后垫，否则后面每行整体错位一天，只能计数报出来。"""
    # bin 起点=2（01-03），只有两格：01-03、01-04；01-01/02 在它前面，01-05 在它后面
    panel = _make_panel(tmp_path / "cn", start=2, close_values=[np.nan, np.nan])
    csv_dir = tmp_path / "csv"
    _write_csv(csv_dir, "SH600001", CAL5, [1.5, 2.0, 3.0, 4.0, 5.0])
    report = ub.fill_holes(panel, csv_dir, CAL5, ["close"])
    got = _read_bin(panel / "features" / "sh600001" / "close.day.bin")
    assert len(got) == 3                             # 长度没变 = 没有往后追加
    assert list(got[1:]) == [3.0, 4.0]               # 覆盖得到的两格补上了
    assert report["out_of_range"] == 3               # 01-01/02（在起点前）+ 01-05（在尾后）
    assert report["filled"] == 2
    # 光有个「14 格补不了」的数字没法查：得说得出是哪只票，否则下次又要从头猜
    assert report["out_of_range_symbols"] == ["SH600001"]


def test_fill_holes_skips_a_symbol_the_panel_does_not_have(tmp_path):
    panel = _make_panel(tmp_path / "cn", start=1, close_values=[1.0])
    csv_dir = tmp_path / "csv"
    _write_csv(csv_dir, "SZ000001", ["2026-01-02"], [2.0])
    report = ub.fill_holes(panel, csv_dir, CAL5, ["close"])
    assert report["filled"] == 0
    assert report["no_feature_dir"] == 1
    assert not (panel / "features" / "sz000001").exists()   # 不替面板凭空造一只票


def test_fill_holes_writes_every_field_it_was_given(tmp_path):
    """因子链读的是 open/high/low/close/volume/amount/vwap/factor，补一个字段就等于少一个字段。"""
    panel = _make_panel(tmp_path / "cn", start=1, close_values=[np.nan, np.nan])
    feats = panel / "features" / "sh600001"
    for field in ("open", "high", "low", "volume", "amount", "vwap", "factor"):
        np.hstack([[1], np.array([np.nan, np.nan], dtype="<f")]).astype("<f").tofile(
            str(feats / f"{field}.day.bin"))
    csv_dir = tmp_path / "csv"
    (csv_dir).mkdir()
    (csv_dir / "SH600001.csv").write_text(
        "date,open,high,low,close,volume,amount,vwap,factor\n"
        "2026-01-02,2,3,1,2.5,100,1000,2.4,1\n"
        "2026-01-03,,,,,,,,\n",
        encoding="utf-8")
    report = ub.fill_holes(panel, csv_dir, CAL5, ["open", "high", "low", "close",
                                                  "volume", "amount", "vwap", "factor"])
    assert report["filled"] == 8
    assert report["by_date"] == {"2026-01-02": 8}
    assert _read_bin(feats / "volume.day.bin")[1] == 100.0
    assert np.isnan(_read_bin(feats / "close.day.bin")[2])   # CSV 里空的行不写


# ------------------------------------------------- amount 回来了，vwap 得跟着改对

def test_a_repaired_amount_rewrites_the_vwap_beside_it(tmp_path):
    """补上 amount 的那一天，同一天的 vwap 即使是 NaN 之外的值也要跟着重写。

    2026-10-23 实测的坑：15:45 的快照合并跑在沪市落库（16:24~16:31）前面，所以 17:15 增量
    拿到的沪市当天是「有价无额」，`dense_frame` 就把 vwap 兜底成 close 写进面板。第二天
    amount 补得回来（那格是 NaN），vwap 那格却因为已经有读数而永远留着 close——
    VWAP0=`$vwap/$close` 于是被钉在 1.0，而真实偏离中位是 0.66 个标准差。
    """
    panel = _make_panel(tmp_path / "cn", start=1, close_values=[1.0, 2.0, 3.0])
    amount = _panel_field(panel, "amount", start=1, values=[np.nan, 5e6, 6e6])
    vwap = _panel_field(panel, "vwap", start=1, values=[1.0, 2.4, 3.1])   # 第 1 格 = 那天的 close 兜底
    csv_dir = tmp_path / "csv"
    _write_days(csv_dir, "SH600001", CAL5[1:4], [1e7, 5e6, 6e6], [1.98, 2.4, 3.1])
    report = ub.fill_holes(panel, csv_dir, CAL5, ["amount", "vwap"])
    got_amount, got_vwap = _read_bin(amount), _read_bin(vwap)
    assert got_amount[1] == 1e7                     # 空洞补上了
    assert got_vwap[1] == pytest.approx(1.98)       # 同一天的 vwap 跟着改对，不是留着 close
    # bin 存的是 float32，比较一律带近似，否则 2.4 这种字面量永远对不上
    assert got_vwap[2] == pytest.approx(2.4) and got_vwap[3] == pytest.approx(3.1)   # 只动补了额那天
    assert report["filled"] == 1
    assert report["derived"] == 1                   # 这次是「跟着 amount 重写的派生格」，要能分开数
    assert report["derived_dates"] == {"2026-01-02": 1}


def test_an_amount_that_was_already_there_leaves_vwap_alone(tmp_path):
    """amount 本来就有值 = 那天的 vwap 是真读数，一个字都不能改（补洞不是二次覆盖）。"""
    panel = _make_panel(tmp_path / "cn", start=1, close_values=[1.0, 2.0])
    amount = _panel_field(panel, "amount", start=1, values=[9e6, 5e6])
    vwap = _panel_field(panel, "vwap", start=1, values=[1.37, 2.4])
    csv_dir = tmp_path / "csv"
    _write_days(csv_dir, "SH600001", CAL5[1:3], [1e7, 5e6], [1.98, 2.4])
    report = ub.fill_holes(panel, csv_dir, CAL5, ["amount", "vwap"])
    assert _read_bin(amount)[1] == 9e6              # 已有的额不改
    assert _read_bin(vwap)[1] == pytest.approx(1.37)   # 配的 vwap 也不改
    assert report["filled"] == 0
    assert report["derived"] == 0


def test_a_missing_vwap_value_does_not_erase_the_panel(tmp_path):
    """窗口 CSV 那天算不出 vwap（额补上了但量为空）→ 保持原格，不能写 NaN 把好读数擦掉。"""
    panel = _make_panel(tmp_path / "cn", start=1, close_values=[1.0])
    amount = _panel_field(panel, "amount", start=1, values=[np.nan])
    vwap = _panel_field(panel, "vwap", start=1, values=[1.0])
    csv_dir = tmp_path / "csv"
    _write_days(csv_dir, "SH600001", CAL5[1:2], [1e7], [None])
    report = ub.fill_holes(panel, csv_dir, CAL5, ["amount", "vwap"])
    assert _read_bin(amount)[1] == 1e7
    assert _read_bin(vwap)[1] == 1.0                # 没得对就不改
    assert report["derived"] == 0


# ---------------------------------------------------------------- 打分侧

def test_thin_ranking_is_a_problem():
    from stock_selector_qlib import serving

    p = serving.ranking_coverage_problem("2026-10-22", ranked=1554, pool=2882)
    assert p is not None
    assert (p["ranked"], p["pool"]) == (1554, 2882)
    assert p["ratio"] == pytest.approx(0.539, abs=0.001)
    assert serving.ranking_coverage_problem("2026-10-22", ranked=2800, pool=2882) is None


def test_an_empty_pool_is_not_reported_as_a_coverage_problem():
    """名单本身为空是另一回事（上游会硬错），这里不能拿它除出个 0% 来刷屏。"""
    from stock_selector_qlib import serving

    assert serving.ranking_coverage_problem("2026-10-22", ranked=0, pool=0) is None


def test_the_same_day_alerts_only_once(monkeypatch):
    from stock_selector_qlib import serving

    sent: list[str] = []
    monkeypatch.setattr(serving, "_send_alert", lambda title, text: sent.append(text))
    serving._alerted_dates.clear()
    problem = serving.ranking_coverage_problem("2026-10-22", ranked=1554, pool=2882)
    assert serving.alert_ranking_coverage(problem) is True
    assert serving.alert_ranking_coverage(problem) is False   # 同一天第二次：闭嘴
    assert len(sent) == 1
    assert "1554" in sent[0] and "2882" in sent[0]
    serving._alerted_dates.clear()
