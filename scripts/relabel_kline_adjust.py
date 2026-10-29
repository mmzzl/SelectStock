"""把 `stock_kline` 里 1,227 万行的 `adjust` 标签从错标的 `qfq` 回补成 `raw`（A1-c，docs §48.4）。

标签为什么错（§48.1）：主源腾讯的请求参数写的是 `qfq`，代码里 `data.get("qfqday") or data.get("day")`
会静默回落到裸价那一组，备源 sina 的日线接口从来不复权——两条路都照样写 `adjust:"qfq"`。
价到底是什么口径不靠嘴说，是拿事件日历验的：2025 年以来 2,405 个除权日（东财分红送配按交易所公式
推的台阶）对上库里那天的价，残差中位 +0.17%~+0.25%，而当天派息/送转的隐含跌幅中位是 0.87%~1.08%。
真前复权的话残差会等于那个隐含跌幅（假跌被复权吃掉），所以库里存的就是裸价。

爬虫已经改成只请求/只认 `day` 并标 `raw`（`kline_spider.py`），这里补历史。默认 **dry-run 只数不改**，
`--apply` 才写；分批按 code 走 `code_1_...` 索引，一笔 `update_many` 只碰「确实还标着 qfq」的行，
重复跑安全（第二遍 matched 归零）。
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pymongo
import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

COL_KLINE = "stock_kline"
FROM_LABEL = "qfq"
TO_LABEL = "raw"
BATCH = 50                   # 一批 50 只票 ≈ 8~9 万行，一笔 update_many 别做成 12M 行的单体事务
FREQUENCIES = (0, 9)         # 日线两批都标错过；分钟线不在这次范围

logger = logging.getLogger("relabel_kline")


def chunks(codes, size: int = BATCH):
    for i in range(0, len(codes), size):
        yield list(codes[i: i + size])


def relabel_chunk(col, batch) -> int:
    """一批票的标签回补，返回真正改动的行数。查询锁死 `adjust:FROM_LABEL`，已经对的一行不碰。"""
    res = col.update_many({"code": {"$in": list(batch)}, "adjust": FROM_LABEL},
                          {"$set": {"adjust": TO_LABEL}})
    return int(res.modified_count)


def mongo_db():
    cfg_path = ROOT / "stock_selector" / "config" / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    m = (cfg or {}).get("mongodb") or {}
    client = pymongo.MongoClient(host=m.get("host", "127.0.0.1"),
                                 port=int(m.get("port", 27017)),
                                 serverSelectionTimeoutMS=8000)
    return client[m.get("database", "eastmoney_news")]


def label_counts(col) -> dict:
    pipe = [{"$match": {"frequency": {"$in": list(FREQUENCIES)}}},
            {"$group": {"_id": "$adjust", "n": {"$sum": 1}}}]
    return {d["_id"]: d["n"] for d in col.aggregate(pipe)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="真写（默认只统计要改多少行）")
    ap.add_argument("--batch", type=int, default=BATCH)
    ap.add_argument("--codes", help="逗号分隔白名单（冒烟用，默认全量）")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db = mongo_db()
    col = db[COL_KLINE]
    print(f"[relabel] 动手前的标签分布（frequency∈{FREQUENCIES}，日线两批）：{label_counts(col)}")

    codes = (args.codes.split(",") if args.codes
             else sorted(col.distinct("code", {"frequency": {"$in": list(FREQUENCIES)}})))
    plan = list(chunks(codes, args.batch))
    mode = "APPLY 写入" if args.apply else "DRY-RUN 只统计"
    print(f"  人口 {len(codes)} 票 / {len(plan)} 批（每批 ≤{args.batch} 只）｜{mode}｜"
          f" {FROM_LABEL} → {TO_LABEL}")

    total = 0
    for i, batch in enumerate(plan, 1):
        if args.apply:
            total += relabel_chunk(col, batch)
        else:
            total += col.count_documents({"code": {"$in": batch}, "adjust": FROM_LABEL})
        if i % 10 == 0 or i == len(plan):
            print(f"  [{i}/{len(plan)}] 累计 {total:,} 行")

    print(f"[relabel] {'已改' if args.apply else '待改'} {total:,} 行")
    print(f"  现在的分布：{label_counts(col)}")
    if not args.apply:
        print("  没动数据：确认数字对了再加 --apply（12M 行的大写不可撤销，跑之前先备份/别赶时间）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
