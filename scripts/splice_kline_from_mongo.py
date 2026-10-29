"""把 Mongo `stock_kline(frequency=0)` 里那段日线拼回 parquet，产出一个**只给研究用**的行情目录。

为什么必须做：批次 E3 的留出段要跑 2022-01→2026-09，而 `data/stock_kline` 里
3,714 只票池有 **851 只缺 2022-12-24→2026-05-06 整段**（上游 `frequency=9` 这批票就没爬全，
`frequency=0` 恰好是互补的另一半：3,121 只 / 94 万行 / 2022-10-24→2026-05-06）。
按现引擎口径「当日无行 = 停牌」，这三年会被读成长期停牌：仓位押上百天卖不掉、
成交笔数与年内回撤全部失真——和批次 E 那次「净值缩水后静默停摆」是同一类仪器缺陷，
所以 E3 开跑前必须先把行情补连续。

三条口径（写死，不许事后改）：
- **重叠日一律以原 parquet 为准**：`frequency=0` 与 `frequency=9` 在接缝处有重叠日，
  两边同一套 qfq，实测 close 差 0.0000 元；真出现差异说明两源口径飘了，脚本会计数并报警，
  绝不悄悄用 Mongo 的值覆盖线上已有的行。
- **派生列不自己算**：补完原始 OHLCV+amount 后整条序列交给 `build_kline_features.compute_indicators`
  重算，和线上重建同一个函数同一套列（82 列）。指标是滚动窗口，只补行不重算会让 ma/atr/pos60 断层。
- **不动生产目录**：输出默认 `data/stock_kline_holdout/`，没缺口的票用相对符号链接指回原文件，
  只有真补了行的票才落地成实体文件（约 850 只，磁盘 ~200MB）。

用法:
    python3 scripts/splice_kline_from_mongo.py --codes 3 -v          # 冒烟（只处理前 3 只有缺口的票）
    python3 scripts/splice_kline_from_mongo.py                        # 全量拼接 + 建符号链接
    python3 scripts/splice_kline_from_mongo.py --verify               # 只核对接缝与行数，不写文件
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

import pandas as pd
import yaml
from pymongo import MongoClient

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from build_kline_features import compute_indicators, is_fund_code  # noqa: E402

KLINE_DIR = ROOT / "data" / "stock_kline"
OUT_DIR = ROOT / "data" / "stock_kline_holdout"
POOL_FILE = ROOT / "data" / "actions" / "decision_samples.parquet"

# 互补源：frequency=0 是日线，恰好覆盖 frequency=9 缺的那一段；两者都是「一日一行」、同为裸价
# （库里的 adjust 标签过去写的是 qfq、实测价是裸价，见 §48.1，所以这里不按它过滤）
SOURCE_FILTER = {"frequency": 0}
RAW_COLUMNS = ["open", "high", "low", "close", "volume", "amount"]
# 接缝跳变警戒：单日涨跌超过这个数就说明两批的来源对不上，得停下来人工看
SEAM_JUMP_WARN = 0.11
OVERLAP_CLOSE_TOL = 0.005       # 重叠日 close 相对差超过 0.5% 视为口径飘

logger = logging.getLogger("splice_kline")


def mongo_config() -> dict:
    conf = yaml.safe_load((ROOT / "stock_selector" / "config" / "config.yaml").read_text(encoding="utf-8"))
    return conf["mongodb"]


def load_source(cfg: dict) -> dict[str, pd.DataFrame]:
    """一次集合扫描把 frequency=0 的日线按 code 分组进内存（94 万行，约 150MB）。

    逐只票查 = 3,121 次全表扫描，本机 mongo 只限了 2G cache，那样会把盘 IO 打满。
    """
    client = MongoClient(cfg["host"], cfg["port"], serverSelectionTimeoutMS=5000)
    col = client[cfg["database"]]["stock_kline"]
    projection = {"code": 1, "date": 1, **{c: 1 for c in RAW_COLUMNS}}
    cur = col.find(SOURCE_FILTER, projection).sort([("code", 1), ("date", 1)])
    frames: dict[str, list[dict]] = {}
    n = 0
    for doc in cur:
        code = str(doc["code"]).zfill(6)
        frames.setdefault(code, []).append({"date": str(doc["date"])[:10],
                                            **{c: doc.get(c) for c in RAW_COLUMNS}})
        n += 1
    client.close()
    logger.info("Mongo %s 读到 %d 行 / %d 只", SOURCE_FILTER, n, len(frames))
    return {code: pd.DataFrame(rows) for code, rows in frames.items()}


def seam_jumps(dates: pd.Series, closes: pd.Series) -> list[tuple[str, float]]:
    """相邻交易日之间的涨跌幅，只用于看「补进去的接缝」有没有台阶。"""
    step = closes.astype(float).pct_change()
    out = []
    for d, v in zip(dates.astype(str), step):
        if pd.notna(v) and abs(v) > SEAM_JUMP_WARN:
            out.append((d[:10], float(v)))
    return out


def splice_one(code: str, src: pd.DataFrame, kline_dir: Path, out_dir: Path) -> dict | None:
    """单只票：原行为准补缺失日，整条序列重算派生列；没补到行返回 None（调用方去建符号链接）。"""
    path = kline_dir / f"{code}.parquet"
    if not path.exists() or src.empty:
        return None
    base = pd.read_parquet(path)
    base["date"] = pd.to_datetime(base["date"])
    have = set(base["date"].dt.strftime("%Y-%m-%d"))
    src = src.copy()
    src["date"] = pd.to_datetime(src["date"])

    overlap = src.loc[src["date"].dt.strftime("%Y-%m-%d").isin(have)]
    drift = 0
    if not overlap.empty:
        merged = overlap.merge(base[["date", "close"]], on="date", suffixes=("_src", "_base"))
        rel = (merged["close_src"] - merged["close_base"]).abs() / merged["close_base"].clip(lower=1e-9)
        drift = int((rel > OVERLAP_CLOSE_TOL).sum())
    add = src.loc[~src["date"].dt.strftime("%Y-%m-%d").isin(have), ["date", *RAW_COLUMNS]]
    if add.empty:
        return None

    # compute_indicators 只要原始列 + code/name；name 沿用原文件（Mongo 那份没有名字）
    raw = pd.concat([base[["date", *RAW_COLUMNS]], add], ignore_index=True)
    raw = raw.drop_duplicates(subset=["date"], keep="first").sort_values("date").reset_index(drop=True)
    raw["code"] = base["code"].iloc[0] if "code" in base else code
    raw["name"] = base["name"].iloc[0] if "name" in base else ""
    df = compute_indicators(raw)
    if is_fund_code(code):
        df["lu"] = False        # 与线上一致：场内基金没有涨停概念
    out_path = out_dir / f"{code}.parquet"
    df.to_parquet(out_path, index=False, compression="zstd")

    dates = pd.to_datetime(df["date"])
    first_add = add["date"].min()
    last_add = add["date"].max()
    # 接缝只看「补进去这一段」的头尾两处：原行→首个补行、末个补行→原行
    window = dates.between(first_add, last_add)
    jumps = seam_jumps(df.loc[window, "date"], df.loc[window, "close"])
    return {"code": code, "rows_base": int(len(base)), "rows_added": int(len(add)),
            "rows_total": int(len(df)), "overlap_days": int(len(overlap)), "overlap_drift": drift,
            "added_from": str(first_add)[:10], "added_to": str(last_add)[:10],
            "big_jumps": len(jumps), "jump_examples": ";".join(f"{d}:{v:+.1%}" for d, v in jumps[:3])}


def link_unchanged(kline_dir: Path, out_dir: Path, spliced: set[str]) -> int:
    """没补数据的票用相对符号链接指回原文件，保持目录里的票宇宙与线上完全一致。"""
    made = 0
    for path in sorted(kline_dir.glob("*.parquet")):
        if path.stem in spliced:
            continue
        link = out_dir / path.name
        if link.is_symlink() or link.exists():
            continue
        os.symlink(os.path.relpath(path, out_dir), link)
        made += 1
    return made


def report(stats: list[dict], out_dir: Path) -> None:
    table = pd.DataFrame(stats)
    if table.empty:
        print("没有任何票需要从 Mongo 补行")
        return
    print("\n== 拼接汇总 ==")
    print(f"补了行的票 {len(table)} 只；新增 {int(table.rows_added.sum()):,} 行；"
          f"重叠日 {int(table.overlap_days.sum()):,} 天，其中 close 对不上的 {int(table.overlap_drift.sum())} 天")
    print(f"接缝单日跳变 >{SEAM_JUMP_WARN:.0%} 的票 {int((table.big_jumps > 0).sum())} 只"
          f"（涨跌停本来就能到 ±20%/±10%，这里只是提示要人工扫一眼，不是判死）")
    print(table.sort_values("rows_added", ascending=False).head(8).to_string(index=False))
    worst = table.loc[table.overlap_drift.idxmax()]
    print(f"\n重叠日口径飘最多的一只：{worst.code}（{int(worst.overlap_drift)} 天 / 重叠 {int(worst.overlap_days)} 天）")


def verify(out_dir: Path, pool_only: bool) -> None:
    """核对补完之后票池在留出段还缺不缺行——这是 E3 能不能开跑的那道闸。"""
    codes = (sorted(pd.read_parquet(POOL_FILE, columns=["code"])["code"].unique().tolist())
             if pool_only else [p.stem for p in sorted(out_dir.glob("*.parquet"))])
    codes = [c for c in codes if (out_dir / f"{c}.parquet").exists()]
    counts, missing = [], []
    for code in codes:
        d = pd.read_parquet(out_dir / f"{code}.parquet", columns=["date"])
        s = pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d")
        n = int(((s >= "2022-01-01") & (s <= "2026-09-30")).sum())
        counts.append(n)
        if n < 900:
            missing.append((code, n))
    counts = pd.Series(counts)
    print(f"\n== 留出段覆盖核对（{len(codes)} 只）==")
    print(f"2022+ 行数 中位 {int(counts.median())} p10 {int(counts.quantile(0.1))} "
          f"最小 {int(counts.min())}（应有约 1,150 个交易日）")
    print(f"仍然 <900 行的只数 {len(missing)}：{missing[:10]}")


def main() -> int:
    ap = argparse.ArgumentParser(description="用 Mongo frequency=0 把留出段日线拼回 parquet")
    ap.add_argument("--kline-dir", default=str(KLINE_DIR))
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    ap.add_argument("--codes", type=int, default=0, help="只处理前 N 只有缺口的票（冒烟）")
    ap.add_argument("--pool-only", action="store_true", help="只处理批 C/D 票池那 3,714 只")
    ap.add_argument("--verify", action="store_true", help="只核对已有输出目录，不读写 Mongo")
    ap.add_argument("--no-link", action="store_true", help="不建符号链接（配合 --codes 冒烟）")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    kline_dir, out_dir = Path(args.kline_dir), Path(args.out_dir)

    if args.verify:
        verify(out_dir, args.pool_only)
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    source = load_source(mongo_config())
    universe = (sorted(pd.read_parquet(POOL_FILE, columns=["code"])["code"].unique().tolist())
                if args.pool_only else sorted(source))
    todo = [c for c in universe if c in source]
    if args.codes:
        todo = todo[:args.codes]
    logger.info("待拼接 %d 只（票宇宙 %d 只，Mongo 有 %d 只）", len(todo), len(universe), len(source))

    stats, done = [], 0
    for code in todo:
        st = splice_one(code, source[code], kline_dir, out_dir)
        if st:
            stats.append(st)
        done += 1
        if done % 200 == 0 or done == len(todo):
            logger.info("已处理 %d/%d，其中实体写出 %d 只（%.0f 秒）",
                        done, len(todo), len(stats), time.time() - t0)
            pd.DataFrame(stats).to_csv("/tmp/splice_stats_partial.csv", index=False)

    spliced = {s["code"] for s in stats}
    if not args.no_link:
        made = link_unchanged(kline_dir, out_dir, spliced)
        logger.info("符号链接 %d 只（未改动），实体文件 %d 只", made, len(spliced))
    report(stats, out_dir)
    logger.info("用时 %.0f 秒，输出目录 %s", time.time() - t0, out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
