"""把 MongoDB 日线导出成 qlib 的 CSV 面板，供 qlib 的 dump_bin.py 转 .bin。

为什么源是 Mongo 而不是 data/stock_kline/ 的 parquet：
`stock_kline` 的日线分成**互补的两批**（实测 301363：`frequency=9` 在 2025 只有 193 根、
缺的 50 天正好全在 `frequency=0` 里，2026 同理 137+51；抽 25 只两批都有的票，
(code,date) 重叠 0 个）。所以两批**必须一起读**：只读 f=9 会得到 600519 这种
「2016→2022 连续、2023-2024 整段空白、2026 才回来」的破面板，训出来的模型全是停牌幻觉。
两批都是 `adjust:"qfq"`、volume 都是「手」，且日期零重叠，因此不存在两套复权基准互相打架。

三条必须照做的口径：
1. **CSV 要按交易日历稠密**：qlib 的 `dump_bin.py dump_all` 只去重、不 reindex，它拿
   「该行在日历里的下标 + 行序」直接写 bin。所以停牌/缺采造成的空档必须由我们在导出
   时填 NaN，否则这只票一旦中间缺过一天，后面每一行的日期都会整体前移——不是丢数据，
   是把 2020 的价格写到 2021 的日期上。区间只到自己最后一根，不越过（否则退市票会被
   当成活票）。
2. **同一 (code,date) 只留 crawl_time 最新的一条**：两批理论零重叠，真撞上时以抓得晚的为准，
   导出末尾会把重叠计数打出来，一旦不是 0 就要停下来查。
3. **volume 换成「股」**：库里是「手」（实测 `amount/(volume×close)` 主板/创业板/科创板/
   场内基金逐年都在 100 上下），qlib 的 cn_data 惯例是股，`vwap = amount / volume(股)`。
   导出前 `--audit-only` 会逐年复核这个比值，改了源就重跑，别照抄注释。
4. **factor 恒为 1**：源已经是 qfq。代价要说清楚——qfq 锚在抓取日，之后每次分红除权
   都会把**整条历史**重新缩放，所以同一套 bin 在不同时间导出来的绝对价位不一样（收益率
   比值不受影响，但跨次比较绝对价时别拿它当尺子）。

用法:
    python3 scripts/qlib_export_kline.py --audit-only          # 只逐年复核量纲，不写文件
    python3 scripts/qlib_export_kline.py --limit 20 -v          # 冒烟：前 20 只
    python3 scripts/qlib_export_kline.py                        # 全量，约 12M 行
    转 bin（不在本脚本里，直接用 qlib 自带的）：
    python3 /data/work/qlib/scripts/dump_bin.py dump_all \\
        --data_path /data/work/qlib_data/csv --qlib_dir /data/work/qlib_data/cn \\
        --freq day --max_workers 4 --include_fields open,high,low,close,volume,amount,vwap,factor
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
import pymongo
import yaml

ROOT = Path(__file__).resolve().parent.parent
CSV_FIELDS = ["date", "open", "high", "low", "close", "volume", "amount", "vwap", "factor"]
OHLC_FIELDS = ("open", "high", "low", "close", "volume", "amount")
# 两批互补的日线：f=9 是主力批次，f=0 是补洞批次（实测两者 (code,date) 零重叠）
FREQUENCIES = [0, 9]
LOT_PER_SHARE = 100.0
# 号段→交易所。5 字头是上海基金、1/2/3 字头归深圳、4/8/9 字头归北交所。
SH_FIRST = set("569")
SZ_FIRST = set("0123")
BJ_FIRST = set("48")
# 比值落在这个带里才认「手」；接近 1 说明那一行给的是「股」。
LOT_BAND = (50.0, 200.0)
SHARE_BAND = (0.5, 2.0)

logger = logging.getLogger("qlib_export")


def mongo_db() -> pymongo.database.Database:
    """连接参数沿用 build_kline_features.py 那份 config，避免两处各连各的库。"""
    cfg_path = ROOT / "stock_selector" / "config" / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    m = (cfg or {}).get("mongodb") or {}
    client = pymongo.MongoClient(
        host=m.get("host", "127.0.0.1"),
        port=int(m.get("port", 27017)),
        serverSelectionTimeoutMS=8000,
    )
    return client[m.get("database", "eastmoney_news")]


def symbol(code: str) -> str:
    head = code[:1]
    if head in BJ_FIRST:
        return f"BJ{code}"
    if head in SH_FIRST:
        return f"SH{code}"
    return f"SZ{code}"


def trading_calendar(col: pymongo.collection.Collection) -> list[str]:
    """全部交易日（升序，纯日期）。走 date_-1 索引，不是在 Python 里扫 1,200 万行。"""
    raw = col.distinct("date", {"frequency": {"$in": FREQUENCIES}})
    return sorted({str(d)[:10] for d in raw})


def dense_frame(rows: list[dict], calendar: list[str], lo: int, hi: int) -> pd.DataFrame:
    """把一只票的原始行摊到 calendar[lo:hi+1] 上，缺失日留 NaN。"""
    # 库里的 date 带「 15:00」后缀，必须先截成纯日期，否则和日历对不上、整列都是 NaN
    by_date: dict[str, dict] = {}
    for r in rows:
        d = str(r["date"])[:10]
        # 同一 (代码,日) 万一两批都有，以抓得晚的那条为准（补洞批次 crawl_time 更新）
        if d not in by_date or str(r.get("crawl_time", "")) > str(by_date[d].get("crawl_time", "")):
            by_date[d] = r
    days = calendar[lo : hi + 1]
    frame = pd.DataFrame(index=pd.Index(days, name="date"), dtype="float64")
    # 逐列直接赋值而不是 join：整段历史都在 --first-year 之前的老退市股 days 与数据无交集，
    # join 会返回一张没有列名的空表，下游 frame["close"] 直接 KeyError
    for field in OHLC_FIELDS:
        frame[field] = [_num(by_date[d].get(field)) if d in by_date else np.nan for d in days]
    frame["volume"] = frame["volume"] * LOT_PER_SHARE
    with np.errstate(divide="ignore", invalid="ignore"):
        vwap = (frame["amount"] / frame["volume"]).where(frame["volume"] > 0)
    # vwap 只在成交额/量都在时才有意义；缺任何一个就退回 close（qlib 用它做日内分布因子）
    frame["vwap"] = vwap.where(vwap.notna() & (vwap > 0), frame["close"])
    frame["factor"] = 1.0
    return frame


def audit(col: pymongo.collection.Collection, codes: list[str], sample: int, seed: int) -> None:
    """逐年看 amount/(volume(手)×close) 的分布：中位应在 100 上下，跑偏就是量纲变了。"""
    rng = np.random.default_rng(seed)
    picked = list(rng.choice(codes, size=min(sample, len(codes)), replace=False))
    stats: dict[str, Counter] = {}
    for i, code in enumerate(picked, 1):
        cur = col.find(
            {"code": code, "frequency": {"$in": FREQUENCIES}},
            {"_id": 0, "date": 1, "close": 1, "volume": 1, "amount": 1},
        ).sort("date", 1)
        for r in cur:
            try:
                ratio = float(r["amount"]) / (float(r["volume"]) * float(r["close"]))
            except (TypeError, ValueError, ZeroDivisionError):
                continue
            if ratio <= 0:
                continue
            year = str(r["date"])[:4]
            band = "手" if LOT_BAND[0] <= ratio <= LOT_BAND[1] else ("股" if SHARE_BAND[0] <= ratio <= SHARE_BAND[1] else "异常")
            stats.setdefault(year, Counter())[band] += 1
        if i % 200 == 0:
            logger.info("自检进度 %d/%d", i, len(picked))
    print(f"{'年份':<6}{'行数':>10}{'像手':>10}{'像股':>10}{'异常':>10}   判定")
    for year in sorted(stats):
        c = stats[year]
        n = sum(c.values())
        verdict = "volume 是手" if c["手"] / max(n, 1) > 0.95 else "★ 要人看"
        print(f"{year:<6}{n:>10,}{c['手']:>10,}{c['股']:>10,}{c['异常']:>10,}   {verdict}")


def export(col: pymongo.collection.Collection, codes: list[str], calendar: list[str],
           out_dir: Path, min_rows: int, first_year: str) -> dict:
    cal_index = {d: i for i, d in enumerate(calendar)}
    out_dir.mkdir(parents=True, exist_ok=True)
    written = skipped_short = dup_rows = hole_cells = span_cells = 0
    for i, code in enumerate(codes, 1):
        rows = list(col.find(
            {"code": code, "frequency": {"$in": FREQUENCIES}},
            {"_id": 0, "date": 1, "crawl_time": 1, "open": 1, "high": 1, "low": 1,
             "close": 1, "volume": 1, "amount": 1},
        ).sort("date", 1))
        # 剔掉价格不成行的脏数据：qlib 的因子链会在 NaN 之后继续算，留着就是假 K 线
        rows = [r for r in rows if all((_num(r.get(k)) or 0) > 0
                                      for k in ("open", "high", "low", "close"))]
        if len(rows) < min_rows:
            skipped_short += 1
            continue
        dates = sorted({str(r["date"])[:10] for r in rows})
        dup_rows += len(rows) - len(dates)
        lo, hi = cal_index[dates[0]], cal_index[dates[-1]]
        if calendar[lo] < first_year:
            lo = next((j for j, d in enumerate(calendar[lo:], lo) if d >= first_year), hi)
        frame = dense_frame(rows, calendar, lo, hi)
        bars = int(frame["close"].notna().sum())
        if bars < min_rows:
            skipped_short += 1
            continue
        span = hi - lo + 1
        span_cells += span
        hole_cells += span - bars
        frame = frame.reset_index()
        frame["date"] = frame["date"].astype(str)
        frame.to_csv(out_dir / f"{symbol(code)}.csv", columns=CSV_FIELDS, index=False,
                    na_rep="", float_format="%.6g")
        written += 1
        if i % 500 == 0:
            logger.info("导出进度 %d/%d（已写 %d）", i, len(codes), written)
    return {"written": written, "skipped_short": skipped_short, "duplicate_rows": dup_rows,
            "hole_rate": round(hole_cells / span_cells, 4) if span_cells else 0.0,
            "out_dir": str(out_dir)}


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return np.nan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="/data/work/qlib_data/csv", help="CSV 面板目录")
    parser.add_argument("--min-rows", type=int, default=60, help="少于这么多根就跳过（新股/退市壳）")
    parser.add_argument("--first-year", default="2015", help="早于这一年的日历段不导出")
    parser.add_argument("--limit", type=int, default=0, help="只导出前 N 只（冒烟用）")
    parser.add_argument("--codes", default="", help="逗号分隔的代码清单，指定后忽略 --limit")
    parser.add_argument("--audit-only", action="store_true", help="只做量纲自检，不写 CSV")
    parser.add_argument("--audit-sample", type=int, default=300)
    parser.add_argument("--seed", type=int, default=20261018)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    db = mongo_db()
    col = db["stock_kline"]
    calendar = trading_calendar(col)
    print(f"交易日历：{len(calendar)} 天，{calendar[0]} → {calendar[-1]}")
    codes = sorted(col.distinct("code", {"frequency": {"$in": FREQUENCIES}}))
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    elif args.limit:
        codes = codes[: args.limit]
    print(f"票池：{len(codes)} 只（frequency∈{FREQUENCIES}, adjust=qfq）")

    if args.audit_only:
        audit(col, codes, args.audit_sample, args.seed)
        return 0
    result = export(col, codes, calendar, Path(args.out), args.min_rows, args.first_year)
    print("导出完成：" + ", ".join(f"{k}={v:,}" if isinstance(v, int) else f"{k}={v}"
                                   for k, v in result.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
