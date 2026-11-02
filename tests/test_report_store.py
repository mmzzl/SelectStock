"""研报采集层：字段归一、评级方向反推、财年锚、缺数标记、幂等去重。

网络全部注入掉，用例不碰东财。这层存在的理由：东财一次给 51 列，vendored akshare 只映射
13 列且把列名硬编码成 2023/2024（实际含义是「今年/明年」），直接抄会把 2026 年的预测标成
2023 年；而「同一票、同一财年的预测谁高谁低」恰恰是研报里唯一可能有用的信息，锚一错整条链就废。

实测依据（2026-10-30 抽样 500 份个股研报）：`emRatingValue` 中性=0 < 增持=2 < 买入=3，
93.6% 的报告 ratingChange=3 且评级与上次相同（维持），一份都看不到低于中性的评级。
⇒ 方向只能由前后两个数值比出来，不能靠评级名好看与否猜。
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stock_selector.report import store  # noqa: E402, F401

COLLECTED_AT = "2026-10-30 17:30:00"


def raw_report(**overrides):
    """按 2026-10-30 实测抓到的真实报文构造一份「东海证券-恒力石化」研报（字段名照接口原样）。"""
    base = {
        "infoCode": "AP202610301831206048",
        "stockCode": "600346",
        "stockName": "恒力石化",
        "market": "SHANGHAI",
        "title": "公司简评报告：价差修复驱动同比高增，现金流与资产负债表改善",
        "orgCode": "80000069",
        "orgName": "东海证券股份有限公司",
        "orgSName": "东海证券",
        "orgType": "white",
        "researcher": "吴骏燕,谢建斌",
        "publishDate": "2026-10-30 00:00:00.000",
        "indvInduCode": "1274",
        "indvInduName": "炼化及贸易",
        "emRatingName": "买入",
        "emRatingValue": "3",
        "lastEmRatingName": "买入",
        "lastEmRatingValue": "3",
        "ratingChange": 3,
        "reportType": 2,
        "predictThisYearEps": "1.65",
        "predictThisYearPe": "9.54",
        "predictNextYearEps": "1.74",
        "predictNextYearPe": "9.05",
        "predictNextTwoYearEps": "1.85",
        "predictNextTwoYearPe": "8.5",
        "predictLastYearEps": "",
        "actualLastYearEps": "",
        "count": 2,
        "attachPages": 3,
    }
    base.update(overrides)
    return base


# ------------------------------------------------------------------ 数值与空值

def test_接口里的空串归一成_None_而不是假装_0():
    """「这一档没有盈利预测」与「预测恰好为 0」在因子里是两件事，填 0 会把没预测的报告
    伪装成预测为零，之后算修订幅度就会算出假的巨幅上调。"""
    r = store.normalize_report(raw_report(), COLLECTED_AT)
    assert r["eps_fy0"] == 1.65
    assert r["pe_fy0"] == 9.54

    missing = store.normalize_report(
        raw_report(predictNextTwoYearEps="", predictNextTwoYearPe="-"), COLLECTED_AT)
    assert missing["eps_fy2"] is None
    assert missing["pe_fy2"] is None


def test_数值列统一成float而info_code保持字符串():
    r = store.normalize_report(raw_report(), COLLECTED_AT)
    assert isinstance(r["eps_fy2"], float)
    assert r["attach_pages"] == 3
    assert r["info_code"] == "AP202610301831206048"


# ------------------------------------------------------------------ 财年锚

def test_相对年份按发布日归一成绝对财年():
    """接口只说「今年/明年/后年」，跨报告不可比；锚到发布年才能把两份报告摆到同一财年上比。"""
    r = store.normalize_report(raw_report(publishDate="2025-11-05 00:00:00.000"), COLLECTED_AT)
    assert r["fiscal_base_year"] == 2025
    assert r["publish_date"] == "2025-11-05"


def test_同一票跨发布日的预测落在可对齐的两列上():
    early = store.normalize_report(
        raw_report(infoCode="A", publishDate="2026-03-01 00:00:00.000",
                   predictThisYearEps="1.20"), COLLECTED_AT)
    late = store.normalize_report(
        raw_report(infoCode="B", publishDate="2026-09-01 00:00:00.000",
                   predictThisYearEps="1.50"), COLLECTED_AT)
    assert early["fiscal_base_year"] == late["fiscal_base_year"] == 2026
    assert late["eps_fy0"] > early["eps_fy0"]     # 上调：同一 FY2026 上后一份更高


# ------------------------------------------------------------------ 评级方向

@pytest.mark.parametrize("cur,last,want", [
    ("3", "2", "上调"),      # 增持 → 买入
    ("2", "3", "下调"),      # 买入 → 增持（实测 500 份里正是 ratingChange=1 那两条）
    ("3", "3", "维持"),
    ("3", "", "首次"),
    ("", "3", None),         # 本次评级缺失 ⇒ 判不了，不许退回上次评级瞎猜
])
def test_评级方向由前后数值比出(cur, last, want):
    assert store.rating_direction(cur, last) == want


def test_方向与东财自带的编码并存且以数值比为准():
    """ratingChange 是接口自带的枚举（实测 3=维持、2=首次、1=下调），但编码含义没公开文档；
    原值照存，派生值自己算，两者都留在表里以便日后核。"""
    r = store.normalize_report(raw_report(ratingChange=1, emRatingValue="2",
                                          lastEmRatingValue="3", emRatingName="增持",
                                          lastEmRatingName="买入"), COLLECTED_AT)
    assert r["rating_direction"] == "下调"
    assert r["rating_change_code"] == 1
    assert r["rating"] == "增持" and r["last_rating"] == "买入"


# ------------------------------------------------------------------ 缺数出声

def test_发布日晚于采集日的行标记为不可用而不是丢掉():
    """东财列表会回填：今天可能刷出一条「明天」日期的报告。当天榜单不能当天用（只有日粒度、
    没有时刻），所以它必须被标记出来，而不是混进可用数据里，也不是无声消失。"""
    r = store.normalize_report(raw_report(publishDate="2026-10-31 00:00:00.000"), COLLECTED_AT)
    assert r["valid"] is False
    assert r["exclude_reason"] == "future_publish"
    assert r["publish_date"] == "2026-10-31"


def test_没有股票代码或没有唯一号的行标记为不可用():
    bad_code = store.normalize_report(raw_report(stockCode=""), COLLECTED_AT)
    bad_key = store.normalize_report(raw_report(infoCode=""), COLLECTED_AT)
    assert (bad_code["valid"], bad_code["exclude_reason"]) == (False, "no_stock_code")
    assert (bad_key["valid"], bad_key["exclude_reason"]) == (False, "no_info_code")


def test_时变字段明确标注它是采集时刻的读数():
    """count 是「近一月该股研报数」，随采集日变动，不是发布日的事实；列名要自己说清这点。"""
    r = store.normalize_report(raw_report(count=7), COLLECTED_AT)
    assert r["report_count_at_collection"] == 7
    assert r["collected_at"] == COLLECTED_AT
    assert "report_count" not in r                # 不许留一个看不出口径的同名列


# ------------------------------------------------------------------ 落盘幂等

def test_按发布日分区落盘且重跑不翻倍(tmp_path):
    rows = [store.normalize_report(raw_report(infoCode="A"), COLLECTED_AT),
            store.normalize_report(raw_report(infoCode="B"), COLLECTED_AT)]
    first = store.save_day(store.to_frame(rows), "2026-10-30", tmp_path)
    again = store.save_day(store.to_frame(rows), "2026-10-30", tmp_path)
    assert Path(first).name == "2026-10-30.parquet"
    assert Path(again).name == "2026-10-30.parquet"
    assert len(pd.read_parquet(again)) == 2


def test_同一info_code再采到时保留最新一份(tmp_path):
    """同一份报告第二次出现时字段可能更全（例如当时 EPS 还没填），合并要留最新而不是留首份。"""
    old = store.save_day(store.to_frame(
        [store.normalize_report(raw_report(infoCode="A", predictThisYearEps="1.00"),
                                COLLECTED_AT)]), "2026-10-30", tmp_path)
    new = store.save_day(store.to_frame(
        [store.normalize_report(raw_report(infoCode="A", predictThisYearEps="1.65"),
                                "2026-10-31 17:30:00")]), "2026-10-30", tmp_path)
    df = pd.read_parquet(new)
    assert len(df) == 1
    assert float(df.iloc[0]["eps_fy0"]) == 1.65
    assert df.iloc[0]["collected_at"] == "2026-10-31 17:30:00"
    assert Path(old).exists()


def test_表里列集合固定且每行都能落进同一顺序(tmp_path):
    """列顺序漂了以后按位置读的老代码会静默错位，所以 schema 要显式固定。"""
    df = store.to_frame([store.normalize_report(raw_report(), COLLECTED_AT)])
    assert list(df.columns) == store.REPORT_COLUMNS
    assert len(df) == 1
