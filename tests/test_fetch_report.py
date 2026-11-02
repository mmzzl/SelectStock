"""研报采集脚本：分页、按日区间、空日与失败日出声、默认不采当天。

网络注入掉（`fetch_json` 可换），用例不碰东财。这层的职责边界：只做「一天一页地取回来、
归一、落盘、把取不到的日子如实报出来」，任何选股口径都不在这里判。

为什么默认不给「今天」这一档：东财只给发布日、不给发布时刻，今天挂出来的报告可能是
收盘后任何时刻挂的，当天就用来选股等于偷看当天。所以窗口一律到昨天为止，真要当天的
报告只能等第二天再用。
"""

import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_SPEC = importlib.util.spec_from_file_location(
    "fetch_report_em", ROOT / "scripts" / "fetch_report_em.py")
fetch = importlib.util.module_from_spec(_SPEC)
sys.modules["fetch_report_em"] = fetch
_SPEC.loader.exec_module(fetch)


def raw(info_code, code="600346", publish="2026-10-29 00:00:00.000"):
    return {"infoCode": info_code, "stockCode": code, "stockName": "恒力石化",
            "market": "SHANGHAI", "title": "公司简评报告", "orgSName": "东海证券",
            "orgName": "东海证券股份有限公司", "publishDate": publish,
            "emRatingName": "买入", "emRatingValue": "3",
            "lastEmRatingName": "买入", "lastEmRatingValue": "3", "ratingChange": 3,
            "predictThisYearEps": "1.65", "predictThisYearPe": "9.54",
            "predictNextYearEps": "", "predictNextYearPe": "",
            "predictNextTwoYearEps": "", "predictNextTwoYearPe": "",
            "count": 2, "attachPages": 3, "indvInduName": "炼化及贸易"}


class FakeEastmoney:
    """假接口：按日返回预置行，记录每次请求的参数，能模拟分页与报错日。"""

    def __init__(self, by_day, page_size=2, boom_days=()):
        self.by_day = by_day
        self.page_size = page_size
        self.boom_days = set(boom_days)
        self.calls = []

    def __call__(self, params):
        self.calls.append(dict(params))
        begin = params["beginTime"]
        if begin in self.boom_days:
            raise RuntimeError("东财返回 5xx")
        rows = self.by_day.get(begin, [])
        page = int(params["pageNo"])
        start = (page - 1) * self.page_size
        chunk = rows[start:start + self.page_size]
        return {"TotalPage": max(1, -(-len(rows) // self.page_size)), "data": chunk}


def test_默认窗口只到昨天_今天不采():
    days = fetch.default_days("2026-10-30", 3)
    assert days == ["2026-10-27", "2026-10-28", "2026-10-29"]


def test_一天一页地取并按发布日分区落盘(tmp_path):
    api = FakeEastmoney({"2026-10-29": [raw("A"), raw("B")], "2026-10-28": [raw("C")]})
    result = fetch.collect_days(["2026-10-28", "2026-10-29"], api, out_dir=tmp_path,
                                page_size=2, collected_at="2026-10-30 17:30:00")
    assert result.rows == 3
    assert sorted(p.name for p in tmp_path.glob("*.parquet")) == [
        "2026-10-28.parquet", "2026-10-29.parquet"]
    df = pd.read_parquet(tmp_path / "2026-10-29.parquet")
    assert list(df["info_code"]) == ["A", "B"]
    assert df.iloc[0]["collected_at"] == "2026-10-30 17:30:00"


def test_翻页翻到TotalPage为止而不是只取第一页(tmp_path):
    api = FakeEastmoney({"2026-10-29": [raw(f"X{i}") for i in range(5)]})
    result = fetch.collect_days(["2026-10-29"], api, out_dir=tmp_path, page_size=2,
                                collected_at="2026-10-30 17:30:00")
    assert result.rows == 5
    assert [c["pageNo"] for c in api.calls] == ["1", "2", "3"]


def test_某天零条要单独报出来且不写空文件(tmp_path):
    """零条与「没去请求」必须能区分：静默的空目录会让断档看起来像那天真没研报。"""
    api = FakeEastmoney({"2026-10-29": [raw("A")]})
    result = fetch.collect_days(["2026-10-28", "2026-10-29"], api, out_dir=tmp_path,
                                page_size=2, collected_at="2026-10-30 17:30:00")
    assert result.empty_days == ["2026-10-28"]
    assert not (tmp_path / "2026-10-28.parquet").exists()


def test_某天报错不影响别天且要留在结果里(tmp_path):
    api = FakeEastmoney({"2026-10-29": [raw("A")], "2026-10-28": [raw("C")]},
                        boom_days=["2026-10-28"])
    result = fetch.collect_days(["2026-10-28", "2026-10-29"], api, out_dir=tmp_path,
                                page_size=2, collected_at="2026-10-30 17:30:00")
    assert result.failed_days == ["2026-10-28"]
    assert result.rows == 1
    assert (tmp_path / "2026-10-29.parquet").exists()


def test_重跑同一区间行数不翻倍(tmp_path):
    api = FakeEastmoney({"2026-10-29": [raw("A"), raw("B")]})
    kw = dict(out_dir=tmp_path, page_size=2, collected_at="2026-10-30 17:30:00")
    fetch.collect_days(["2026-10-29"], api, **kw)
    result = fetch.collect_days(["2026-10-29"], api, **kw)
    assert len(pd.read_parquet(tmp_path / "2026-10-29.parquet")) == 2
    assert result.rows == 2


def test_未来发布日的行照样落盘但标记不可用(tmp_path):
    """接口回填出「比采集日更晚」的报告时不能把它当正常样本，也不能让它无声消失。"""
    api = FakeEastmoney({"2026-10-29": [raw("F", publish="2026-11-02 00:00:00.000")]})
    fetch.collect_days(["2026-10-29"], api, out_dir=tmp_path, page_size=2,
                       collected_at="2026-10-30 17:30:00")
    df = pd.read_parquet(tmp_path / "2026-10-29.parquet")
    assert df["valid"].tolist() == [False]
    assert df["exclude_reason"].tolist() == ["future_publish"]


def test_限速参数会真的传给请求间隔离(tmp_path, monkeypatch):
    """东财这条链踩过 IP 时间窗节流（资金流那侧的教训），一页一停是硬要求，不是可选项。"""
    sleeps = []
    monkeypatch.setattr(fetch.time, "sleep", lambda s: sleeps.append(s))
    api = FakeEastmoney({"2026-10-29": [raw(f"X{i}") for i in range(5)]})
    fetch.collect_days(["2026-10-29"], api, out_dir=tmp_path, page_size=2,
                       collected_at="2026-10-30 17:30:00", delay=0.7)
    assert sleeps and all(s == pytest.approx(0.7) for s in sleeps)
