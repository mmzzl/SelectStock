"""研报存储：东财 51 列报文 → 固定 32 列日分区表。纯函数层，不碰网络。

为什么要这一层（2026-10-30 实测）：

- **列名会骗人**。接口只给「今年/明年/后年」三档 EPS·PE 预测，vendored akshare 把它们硬编码成
  `2023-盈利预测-收益`/`2024-…`，直接抄会把 2026 年的预测标成 2023 年。这里用 `fiscal_base_year`
  （= 发布日所在自然年）把三档钉到绝对财年上：`eps_fy0` 是 `fiscal_base_year` 年，`fy1`/`fy2` 顺推。
  没有这个锚，两份不同日子发布的报告根本摆不到同一财年里比。
- **评级几乎没有区分度，只有方向有**。抽样 500 份个股研报：`emRatingValue` 中性=0 < 增持=2 < 买入=3，
  468/500 是「与上次相同」（`ratingChange=3`），一份低于中性的都没有。所以表里同时留 `rating_value`
  和由前后数值比出来的 `rating_direction`；方向自己算，`rating_change_code` 原样留着以便日后对账
  （东财没公布那套枚举的文档）。
- **EPS×PE 不是目标价**。实测三份预测各自 EPS×PE 都等于报告日股价（恒力石化 1.65×9.54、1.74×9.05、
  1.85×8.5 全 ≈ 15.7），所以这一层不生成任何「目标价/隐含空间」列，避免下游把它当成上涨空间。
- **有些列是时变的**。`count` 是「近一月该股研报数」，随采集日变动而不是发布日的事实，列名叫
  `report_count_at_collection`；换 `collected_at` 一起看才知道它是哪一刻的读数。
- **只有日粒度、没有时刻**。发布日写「今天」的报告可能是今天任何时刻挂上去的，当天用就是前瞻。
  所以 `publish_date > collected_at` 当日的那一行不丢弃，而是标 `valid=False` +
  `future_publish`：静默丢掉会让「今天没研报」和「今天采到但不许用」变成同一件事，谁也都查不出来。
- 300/688 不在这层剔。§30 的「剔创业/科创」是选股人群口径，数据层留全量，剔与不剔留给读时。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

REPORT_COLUMNS = [
    "publish_date", "info_code", "code", "name", "market",
    "title", "org_sname", "org_name", "org_code", "org_type", "researcher",
    "industry_code", "industry_name", "report_type",
    "rating", "rating_value", "last_rating", "last_rating_value",
    "rating_change_code", "rating_direction",
    "fiscal_base_year", "eps_fy0", "pe_fy0", "eps_fy1", "pe_fy1", "eps_fy2", "pe_fy2",
    "report_count_at_collection", "attach_pages",
    "valid", "exclude_reason", "collected_at",
]

# 结构性不可用的三种情形，分开记：以后「今天行数少了」要能一眼看出是接口没给号、
# 还是日期跑到了采集日之后，而不是都归成一句「有脏数据」。
REASON_NO_INFO_CODE = "no_info_code"
REASON_NO_STOCK_CODE = "no_stock_code"
REASON_NO_PUBLISH_DATE = "no_publish_date"
REASON_FUTURE_PUBLISH = "future_publish"

DIRECTION_UPGRADE = "上调"
DIRECTION_DOWNGRADE = "下调"
DIRECTION_MAINTAIN = "维持"
DIRECTION_FIRST = "首次"

# 空值标记：接口对「这一档没预测」发的是 "" 或 "-"，两种都要归一成 None。
_NULL_TOKENS = ("", "-", "None", "NaN", None)


def _num(value):
    """数值列：""/"-"/非数字一律 None，绝不填 0（缺预测与预测为零是两件事）。"""
    if value in _NULL_TOKENS:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value):
    n = _num(value)
    return None if n is None else int(n)


def _text(value):
    if value in _NULL_TOKENS:
        return None
    s = str(value).strip()
    return s or None


def _publish_date(raw_value):
    """接口给的是 `2026-10-30 00:00:00.000`，只取日期并验证它真是个日期。"""
    s = str(raw_value or "").strip()[:10]
    try:
        datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return None
    return s


def rating_direction(cur, last):
    """由前后两个东财评级数值比出方向；本次评级缺失时返回 None，不许退回上次评级瞎猜。

    数值序（实测）：中性 0 < 增持 2 < 买入 3，数值越大越乐观。
    """
    c = _num(cur)
    if c is None:
        return None
    l = _num(last)
    if l is None:
        return DIRECTION_FIRST
    if c > l:
        return DIRECTION_UPGRADE
    if c < l:
        return DIRECTION_DOWNGRADE
    return DIRECTION_MAINTAIN


def normalize_report(raw: dict, collected_at: str) -> dict:
    """一份东财研报报文 → 表里的一行（字段缺失也返回行，用 valid/exclude_reason 说明）。"""
    publish_date = _publish_date(raw.get("publishDate"))
    base_year = int(publish_date[:4]) if publish_date else None
    info_code = _text(raw.get("infoCode"))
    code = _text(raw.get("stockCode"))
    collected_date = str(collected_at or "")[:10]

    reason = None
    if not code:
        reason = REASON_NO_STOCK_CODE
    elif not info_code:
        reason = REASON_NO_INFO_CODE
    elif not publish_date:
        reason = REASON_NO_PUBLISH_DATE
    elif publish_date > collected_date:
        # 发布日跑到采集日之后：多半是接口回填或时区差，留行但判不可用
        reason = REASON_FUTURE_PUBLISH

    return {
        "publish_date": publish_date,
        "info_code": info_code,
        "code": code,
        "name": _text(raw.get("stockName")),
        "market": _text(raw.get("market")),
        "title": _text(raw.get("title")),
        "org_sname": _text(raw.get("orgSName")),
        "org_name": _text(raw.get("orgName")),
        "org_code": _text(raw.get("orgCode")),
        "org_type": _text(raw.get("orgType")),
        "researcher": _text(raw.get("researcher")),
        "industry_code": _text(raw.get("indvInduCode")),
        "industry_name": _text(raw.get("indvInduName")),
        "report_type": _int(raw.get("reportType")),
        "rating": _text(raw.get("emRatingName")),
        "rating_value": _num(raw.get("emRatingValue")),
        "last_rating": _text(raw.get("lastEmRatingName")),
        "last_rating_value": _num(raw.get("lastEmRatingValue")),
        "rating_change_code": _int(raw.get("ratingChange")),
        "rating_direction": rating_direction(raw.get("emRatingValue"),
                                             raw.get("lastEmRatingValue")),
        "fiscal_base_year": base_year,
        "eps_fy0": _num(raw.get("predictThisYearEps")),
        "pe_fy0": _num(raw.get("predictThisYearPe")),
        "eps_fy1": _num(raw.get("predictNextYearEps")),
        "pe_fy1": _num(raw.get("predictNextYearPe")),
        "eps_fy2": _num(raw.get("predictNextTwoYearEps")),
        "pe_fy2": _num(raw.get("predictNextTwoYearPe")),
        "report_count_at_collection": _int(raw.get("count")),
        "attach_pages": _int(raw.get("attachPages")),
        "valid": reason is None,
        "exclude_reason": reason,
        "collected_at": collected_at,
    }


def to_frame(rows: list[dict]) -> pd.DataFrame:
    """固定列顺序建表：列漂了会让按位置读的老代码静默错位，所以缺列补 None、多列丢弃。"""
    df = pd.DataFrame(rows)
    for col in REPORT_COLUMNS:
        if col not in df.columns:
            df[col] = None
    return df[REPORT_COLUMNS]


def save_day(df: pd.DataFrame, day: str, out_dir: str | Path) -> Path:
    """按日分区落盘（重跑幂等：同一 info_code 保留最新一份）。

    分区键用请求日（接口就是按 publishDate 过滤那一天），而不是行里的 publishDate：回填出
    更晚日期的异常行会带着 `future_publish` 标记留在请求日的文件里，而不是被藏进一个
    当天没人会去读的未来分区。回测要找「T 日之前已存在的报告」时，按日切文件
    「某天有没有研报」就是一次目录列举，与资金流那层同一判据。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{day}.parquet"
    frame = to_frame(df.to_dict("records")) if len(df) else to_frame([])
    if path.exists():
        old = pd.read_parquet(path)
        frame = pd.concat([old, frame], ignore_index=True)
    frame = frame.drop_duplicates("info_code", keep="last")
    frame = frame.sort_values(["code", "publish_date", "info_code"], na_position="last")
    frame = frame.reset_index(drop=True)
    frame.to_parquet(path, index=False, compression="zstd")
    return path
