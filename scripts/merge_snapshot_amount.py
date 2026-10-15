"""把盘后快照里的精确成交额并回本地日线（`stock_kline.amount`）。

为什么要这一步：日线爬虫的两个源（腾讯 ifzq、新浪 getKLineData）的**日线接口都不给成交额**，
自 2026-05-21 起库里 amount 一直是空/0，下游只能按 volume×close 估算（见
`stock_selector/risk/context.py`）。而腾讯快照的成交额字段是收盘后交易所口径的精确值
（解析见 `stock_selector/quote/tencent.py`），所以 15:30 抓完快照，这里立刻并回当天那根日线，
18:00 的 build_kline_features 自然带上真成交额。

口径：
- 只回写 amount，不动 OHLCV。快照与日线的 code 都是 6 位纯数字，直接对齐；
- amount<=0 跳过：快照里的 0 是停牌/退市占位，落成 0 会被下游当成「真·零成交」，
  留 null 才能让它正常走 volume×close 兜底；
- `upsert=False`：日线行归爬虫建，这里只补字段，绝不凭空造半根 K 线；
- 幂等：同日重跑写同一批值，所以默认往回合并 `--days` 个交易日，爬虫哪天漏了半批
  （比如 2026-10-14 就只有 5872/6598 只落库），第二天调度自己会补上，不用人管。

用法:
    python scripts/merge_snapshot_amount.py                     # 默认今天
    python scripts/merge_snapshot_amount.py --days 5            # 最近 5 个有快照的交易日
    python scripts/merge_snapshot_amount.py --date 2026-10-14
    python scripts/merge_snapshot_amount.py --dry-run
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from pymongo import UpdateOne

from database import get_db

ROOT = Path(__file__).resolve().parent.parent
SNAP_DIR = ROOT / "data" / "market_snapshot"
KLINE_COLLECTION = "stock_kline"

# 日线的 date 是字符串 "YYYY-MM-DD 15:00"（爬虫把每根日 K 钉在收盘时刻，见 kline_spider.TODAY_FLAG）
DATE_SUFFIX = " 15:00"
FREQUENCY = 9          # 只有 9 = 日 K 是爬虫产物，0 是别的频率，别误伤

logger = logging.getLogger("merge_snapshot_amount")


def snapshot_days() -> list[str]:
    return sorted(p.stem for p in SNAP_DIR.glob("*.parquet")
                  if len(p.stem) == 10 and p.stem.replace("-", "").isdigit())


def merge_day(day: str, dry_run: bool = False) -> dict:
    path = SNAP_DIR / f"{day}.parquet"
    if not path.exists():
        logger.warning("%s: 快照文件不存在，跳过（%s）", day, path)
        return {"day": day, "skipped": "no_snapshot", "matched": 0, "written": 0}

    df = pd.read_parquet(path, columns=["code", "amount"])
    df = df[pd.to_numeric(df["amount"], errors="coerce") > 0]
    candidates = int(len(df))

    ops = [
        UpdateOne({"code": str(code).zfill(6), "date": f"{day}{DATE_SUFFIX}",
                   "frequency": FREQUENCY},
                  {"$set": {"amount": float(amount)}}, upsert=False)
        for code, amount in zip(df["code"], df["amount"])
    ]
    if dry_run:
        logger.info("%s: 候选 %d 条（dry-run，未写入）", day, candidates)
        return {"day": day, "candidates": candidates, "matched": 0, "written": 0,
                "skipped": "dry_run"}

    result = get_db()[KLINE_COLLECTION].bulk_write(ops, ordered=False)
    matched, modified = result.matched_count, result.modified_count
    if matched < candidates:
        # 日线行还没落库（爬虫当天失败/晚于本脚本）——只报警，不 upsert 半根 K 线
        logger.warning("%s: 候选 %d 条里只有 %d 条找到对应日线行，缺 %d 条"
                      "（爬虫当天没抓全，往后几天的 --days 会自动补）",
                      day, candidates, matched, candidates - matched)
    logger.info("%s: 候选 %d / 命中 %d / 更新 %d", day, candidates, matched, modified)
    return {"day": day, "candidates": candidates, "matched": matched, "written": modified}


def main() -> int:
    ap = argparse.ArgumentParser(description="把盘后快照的精确成交额并回日线")
    ap.add_argument("--date", help="只合并这一个交易日 YYYY-MM-DD")
    ap.add_argument("--days", type=int, default=1,
                    help="往回合并最近 N 个有快照的交易日（默认 1 = 今天）")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写库")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")

    if args.date:
        days = [args.date]
    else:
        # 按目录里实际存在的快照往回取 N 天（周末/节假日没有文件，自然跳过）
        days = snapshot_days()[-max(args.days, 1):]
    if not days:
        logger.error("%s 下没有快照文件，无事可做", SNAP_DIR)
        return 1

    total = {"candidates": 0, "matched": 0, "written": 0}
    for day in days:
        r = merge_day(day, dry_run=args.dry_run)
        for k in total:
            total[k] += r.get(k, 0) or 0
    logger.info("完成 %d 个交易日：候选 %d / 命中 %d / 更新 %d",
                len(days), total["candidates"], total["matched"], total["written"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
