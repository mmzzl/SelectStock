"""一次性把科创板（688/689）历史日线的成交量从「股」折成「手」。

背景：成交量单位**按板块**不同，不只按数据源。2026-10-15 拿盘后快照的成交额做基准
（`amount / (volume × close)`，库里 volume 若是手则比值贴着 100，若是股贴着 1），逐行分类
全市场 68x 日线得到的结论是：

    legacy 导入 675,731 行 → 手（2026-10-03 之前就在库里，一直没动）
    sina   8,919 行        → 手（2026-10-03 那次按 source 折算过）
    tencent 22,593 行      → 股  ← 腾讯对科创板给的是「股」，其余板块给「手」

所以同一只 688 票的历史里单位是交错的，`vol_ratio` 一跨源就打出 100 倍的锯齿。爬虫侧已经
按号段修好（`kline_spider._tencent_volume`），这里补的是它之前落库的那批。

判定口径：不认 `source`、不认 `vol_unit` 标记，只认**这一行自己的** `成交额/(量×价)`——
因为 2026-10-03 那次给 sina 行盖了 `vol_unit:"lot"`，而同期腾讯的 688 行也被盖了同一个标记
（值却是股），标记不可信；成交额是刚回补过的精确值，拿它当尺子最硬。
- 比值落在 [50, 200] → 已经是手，跳过；
- 比值落在 [0.3, 3] → 是股，÷100 改写；
- 拿不到成交额（停牌行、以及 689 这种 CDR 在回补窗口里腾讯不给额）→ 退一步看**同一代码同一
  来源**其余行的结论，全体一致是「股」才折，否则只告警不动。

可重复执行：÷100 之后比值变成 ~100，下一轮自然归入「已经是手」；原值另存 `volume_raw`，
单位统一打 `vol_unit:"lot"`，所以要回滚就是把 `volume_raw` 写回 `volume`。

用法：
    python scripts/fix_star_volume_unit.py --dry-run          # 先看会动多少行
    python scripts/fix_star_volume_unit.py                    # 落库
    python scripts/fix_star_volume_unit.py --codes 689009     # 只处理指定代码
写完必须 `python scripts/build_kline_features.py --full` 重建 parquet，
否则特征层（vol_ratio / 量能类）还是旧的。
"""

import argparse
import logging
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymongo import UpdateOne

from database import get_db

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"

STAR_PREFIXES = ("688", "689")    # 科创板：688 字头 + 689 字头（CDR）
SHARES_PER_LOT = 100.0
BATCH = 2000

# 单位的判据带：比值贴着 100 是手、贴着 1 是股，两头各留一倍余量。
# 带外的值（0.004 = 指数、NaN）宁可不改，也不要拿它猜单位。
LOT_LOW, LOT_HIGH = 50.0, 200.0
SHARE_LOW, SHARE_HIGH = 0.3, 3.0

logger = logging.getLogger("fix_star_volume_unit")


def classify(amount, volume, close):
    """这一行的 volume 单位：'lot' / 'shares' / 'unknown'（判不了）/ 'skip'（没有量）。"""
    if not volume or volume <= 0:
        return "skip"             # 停牌行：量是 0，折不折都是 0
    if not amount or amount <= 0 or not close or close <= 0:
        return "unknown"
    ratio = amount / (volume * float(close))
    if LOT_LOW <= ratio <= LOT_HIGH:
        return "lot"
    if SHARE_LOW <= ratio <= SHARE_HIGH:
        return "shares"
    return "unknown"


def build_ops(rows, tally, unknowns, stat):
    """按单位分类，累进 (代码,来源) 的判定统计；'股' 的行生成 ÷100 的更新。"""
    ops = []
    for code, source, doc in rows:
        kind = classify(doc.get("amount"), doc.get("volume"), doc.get("close"))
        if kind == "skip":
            stat["zero"] += 1
            continue
        if kind == "unknown":
            unknowns[(code, source)].append((doc["_id"], doc["volume"]))
            continue
        tally[(code, source)][kind] += 1
        if kind == "shares":
            ops.append(make_op(doc["_id"], doc["volume"], code,
                               str(doc["date"])[:10], source))
    return ops


def make_op(doc_id, volume, code, day, source):
    """÷100 的更新。filter 带 _id + 原 volume：并发写进来过就自然失配，不会二次折算。"""
    return UpdateOne({"_id": doc_id, "volume": volume},
                     {"$set": {"volume": int(round(volume / SHARES_PER_LOT)),
                               "volume_raw": volume,
                               "vol_unit": "lot"}})


def resolve_unknowns(unknowns, tally):
    """判不了单位的行（多半是 689 这种腾讯不给成交额的 CDR）：看同代码同来源的整体结论。

    必须按 (代码, 来源) 分组而不是只按代码——同一只 688 票 sina 行是手、tencent 行是股，
    按代码分组永远不「全体一致」，这批就白留了。
    """
    ops = []
    for (code, source), items in unknowns.items():
        verdict = tally.get((code, source), {})
        if not verdict.get("shares") or verdict.get("lot"):
            logger.warning("%s/%s: %d 行没有成交额且同类无一致结论（%s），不动",
                           code, source, len(items), verdict or "无样本")
            continue
        logger.info("%s/%s: %d 行没有成交额，按同类一致结论（股）折算", code, source, len(items))
        for doc_id, volume in items:
            ops.append(make_op(doc_id, volume, code, "", source))
    return ops


def star_codes(codes, prefixes):
    if codes:
        return [c.split(".")[-1].zfill(6) for c in codes]
    cur = get_db()["stock_kline"].distinct(
        "code", {"frequency": 9,
                  "code": {"$regex": f"^({'|'.join(prefixes)})"}})
    return sorted(cur)


def main() -> int:
    ap = argparse.ArgumentParser(description="科创板历史日线成交量 股→手 一次性补数")
    ap.add_argument("--codes", nargs="*", help="只处理这些代码（调试用）")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    ap.add_argument("--no-unknown", action="store_true",
                    help="没有成交额的行一律不动（默认按同类一致结论折算）")
    args = ap.parse_args()

    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(
                      LOG_DIR / f"fix_star_volume_{time.strftime('%Y%m%d')}.log",
                      encoding="utf-8")])

    codes = star_codes(args.codes, STAR_PREFIXES)
    if not codes:
        logger.error("没有科创板代码，退出")
        return 1
    logger.info("扫描 %d 只 68x 代码的日线", len(codes))

    collection = get_db()["stock_kline"]
    tally = defaultdict(lambda: {"lot": 0, "shares": 0})
    unknowns = defaultdict(list)
    stat = defaultdict(int)
    ops = []
    batches = 0
    for code in codes:
        cur = collection.find({"code": code, "frequency": 9},
                              {"_id": 1, "date": 1, "volume": 1, "close": 1,
                               "amount": 1, "source": 1})
        rows = [(code, d.get("source") or "legacy", d) for d in cur]
        new = build_ops(rows, tally, unknowns, stat)
        ops.extend(new)
        stat["rows"] += len(rows)
        stat["shares"] += len(new)
        if len(ops) >= BATCH:
            batches += flush(collection, ops, args)
            ops = []
    if unknowns and not args.no_unknown:
        extra = resolve_unknowns(dict(unknowns), dict(tally))
        ops.extend(extra)
        stat["unknown_fixed"] += len(extra)
    stat["unknown"] += sum(len(v) for v in unknowns.values())
    if ops:
        batches += flush(collection, ops, args)

    logger.info("行数 %d｜已是手 %d｜停牌无量 %d｜折成手 %d（其中无成交额按同类折算 %d）"
                "｜判不了且未动 %d｜批次 %d",
                stat["rows"], tally_total(tally, "lot"), stat["zero"],
                stat["shares"] + stat["unknown_fixed"], stat["unknown_fixed"],
                stat["unknown"] - stat["unknown_fixed"], batches)
    logger.info("完成后请跑：python scripts/build_kline_features.py --full")
    return 0


def tally_total(tally, kind):
    return sum(v[kind] for v in tally.values())


def flush(collection, ops, args) -> int:
    if not ops:
        return 0
    if args.dry_run:
        logger.info("dry-run 待写 %d 行", len(ops))
        return 1
    result = collection.bulk_write(ops, ordered=False)
    logger.info("写入 matched=%d modified=%d", result.matched_count, result.modified_count)
    return 1


if __name__ == "__main__":
    sys.exit(main())
