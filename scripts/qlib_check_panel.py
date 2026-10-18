"""qlib bin 面板 ↔ Mongo 原始日线 逐点等价性校验。

为什么要单独一个脚本：CSV→bin 这一步是 qlib 自己的 `dump_bin.py` 做的，
它按行序写值、不对齐日历，一旦上游 CSV 不是日历稠密的，第 n 行就会悄悄对上第 m 天，
错得没有任何报错。所以转完必须拿原始数据逐点回读核对，不能只看训练能不能跑。

三件事分开判，缺一个都不算过：
1. **逐点值相等**：抽若干 (代码, 交易日)，把 open/high/low/close/volume/amount/vwap/factor
   八个字段和 Mongo 原始行比（volume 要 ×100 换股，vwap 是反算的，factor 恒为 1）。
2. **日历对齐**：整只票的日期序列要一格不差——qlib 读出来的第 n 天必须就是 CSV 的第 n 天。
   这条专门抓「值相等但日期错位」，那是最坏的情况。
3. **区间边界**：`instruments/all.txt` 里每只票的 start/end 要等于它 CSV 的首末行日期。

用法:
    python3 scripts/qlib_check_panel.py --symbols 300 --days-per-symbol 3
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pymongo

QLIB_DIR = Path("/data/work/qlib_data/cn")
CSV_DIR = Path("/data/work/qlib_data/csv")
FREQUENCIES = [0, 9]
LOT_PER_SHARE = 100.0
TOL = 1e-4  # CSV 按 %.6g 落盘、bin 是 float32，比的是同一个数被写过两次的误差
PRICE_FIELDS = ("open", "high", "low", "close", "volume", "amount")
STOCK_PREFIX = ("60", "68", "00", "30")


def qlib_frame(qlib, symbol: str, beg: str, end: str) -> pd.DataFrame:
    """读回 bin，返回带 date 列的扁平表（日期已按 qlib 日历还原）。"""
    cols = ["$open", "$high", "$low", "$close", "$volume", "$amount", "$vwap", "$factor"]
    frame = qlib.data.D.features([symbol], cols, start_time=beg, end_time=end)
    if frame.empty:
        return pd.DataFrame(columns=["date", *cols])
    frame = frame.rename(columns=lambda c: c.lstrip("$"))
    if "datetime" not in frame.index.names:
        raise RuntimeError(f"qlib 返回的索引层名不认识：{frame.index.names}")
    frame["date"] = [str(d)[:10] for d in frame.index.get_level_values("datetime")]
    return frame.reset_index(drop=True)


def mongo_by_date(col, code: str) -> dict[str, dict]:
    """同一 (代码,日) 有重复行时取 crawl_time 最新的那条——和导出脚本同一条规则。"""
    out: dict[str, dict] = {}
    cur = col.find({"code": code, "frequency": {"$in": FREQUENCIES}},
                   {"_id": 0, "date": 1, "crawl_time": 1, "open": 1, "high": 1, "low": 1,
                    "close": 1, "volume": 1, "amount": 1})
    for r in cur:
        d = str(r["date"])[:10]
        if d not in out or str(r.get("crawl_time", "")) > str(out[d].get("crawl_time", "")):
            out[d] = r
    return out


def same(a, b) -> bool:
    if a is None or b is None:
        return (a is None or (isinstance(a, float) and np.isnan(a))) and \
               (b is None or (isinstance(b, float) and np.isnan(b)))
    return abs(float(a) - float(b)) <= TOL * max(1.0, abs(float(b)))


def expect_vwap(raw: dict) -> float:
    volume = float(raw["volume"]) * LOT_PER_SHARE if raw["volume"] is not None else 0.0
    amount = float(raw["amount"]) if raw["amount"] is not None else np.nan
    with np.errstate(divide="ignore", invalid="ignore"):
        vwap = amount / volume if volume > 0 else np.nan
    return vwap if vwap == vwap and vwap > 0 else float(raw["close"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--qlib-dir", default=str(QLIB_DIR))
    ap.add_argument("--csv-dir", default=str(CSV_DIR))
    ap.add_argument("--symbols", type=int, default=300, help="逐点比对抽多少只票")
    ap.add_argument("--days-per-symbol", type=int, default=3, help="每只票抽多少个交易日")
    ap.add_argument("--align-symbols", type=int, default=20, help="日历对齐检查的票数")
    ap.add_argument("--seed", type=int, default=20261018)
    args = ap.parse_args()

    import qlib
    from qlib.constant import REG_CN

    qlib.init(provider_uri=args.qlib_dir, region=REG_CN, kernels=1,
              expression_cache=None, dataset_cache=None)
    mongo_col = (pymongo.MongoClient("127.0.0.1", 27017, serverSelectionTimeoutMS=8000)
                 ["eastmoney_news"]["stock_kline"])

    csv_dir = Path(args.csv_dir)
    files = sorted(f for f in csv_dir.glob("*.csv") if f.stem[2:4] in STOCK_PREFIX)
    rng = np.random.default_rng(args.seed)
    print(f"面板 {len(files)} 只股票；种子 {args.seed}；票池与抽样口径同导出脚本")

    # ---- 1. 逐点值等价 ----
    picks = list(rng.choice(files, size=min(args.symbols, len(files)), replace=False))
    fields_checked = days_checked = mismatches = 0
    bad: list[str] = []
    for path in picks:
        symbol, code = path.stem, path.stem[2:]
        frame = qlib_frame(qlib, symbol, "2015-01-01", "2026-12-31")
        if frame.empty:
            mismatches += 1
            bad.append(f"{symbol}: qlib 整只读不到")
            continue
        src = mongo_by_date(mongo_col, code)
        rows = frame[frame["date"].isin(src.keys())]
        if rows.empty:
            mismatches += 1
            bad.append(f"{symbol}: qlib 有 {len(frame)} 行但没有一行落在 Mongo 的日期里")
            continue
        sample = rows.sample(n=min(args.days_per_symbol, len(rows)), random_state=int(rng.integers(1e9)))
        for _, row in sample.iterrows():
            raw = src[row["date"]]
            days_checked += 1
            for field in PRICE_FIELDS:
                expect = float(raw[field]) * (LOT_PER_SHARE if field == "volume" else 1.0)
                fields_checked += 1
                if not same(row[field], expect):
                    mismatches += 1
                    if len(bad) < 12:
                        bad.append(f"{symbol} {row['date']} {field}: qlib={row[field]} mongo={expect}")
            for field, expect in (("vwap", expect_vwap(raw)), ("factor", 1.0)):
                fields_checked += 1
                if not same(row[field], expect):
                    mismatches += 1
                    if len(bad) < 12:
                        bad.append(f"{symbol} {row['date']} {field}: qlib={row[field]} 期望={expect}")
    print(f"[1] 逐点值：{len(picks)} 只票 / {days_checked:,} 个交易日 / "
          f"{fields_checked:,} 个字段值 / 不符 {mismatches:,}")
    for line in bad[:12]:
        print("    ✗", line)

    # ---- 2. 日历对齐（整只票逐格比） ----
    align_files = list(rng.choice(files, size=min(args.align_symbols, len(files)),
                                 replace=False))
    align_bad = 0
    for path in align_files:
        symbol = path.stem
        csv_dates = pd.read_csv(path, usecols=["date"])["date"].astype(str).tolist()
        frame = qlib_frame(qlib, symbol, csv_dates[0], csv_dates[-1])
        qlib_dates = frame["date"].tolist()
        if qlib_dates != csv_dates:
            align_bad += 1
            first = next((i for i, (a, b) in enumerate(zip(qlib_dates, csv_dates)) if a != b),
                         min(len(qlib_dates), len(csv_dates)))
            print(f"    ✗ {symbol} 第 {first} 格起错位：qlib={qlib_dates[first] if first < len(qlib_dates) else 'EOF'}"
                  f" csv={csv_dates[first] if first < len(csv_dates) else 'EOF'}")
    print(f"[2] 日历对齐：抽 {len(align_files)} 只整票逐格比，错位 {align_bad} 只")

    # ---- 3. instruments 区间边界 ----
    spans = {}
    for line in (Path(args.qlib_dir) / "instruments" / "all.txt").read_text().splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            spans[parts[0].strip().upper()] = (parts[1].strip(), parts[2].strip())
    inst_bad = 0
    for path in align_files:
        symbol = path.stem
        csv_dates = pd.read_csv(path, usecols=["date"])["date"].astype(str).tolist()
        if symbol not in spans:
            inst_bad += 1
            print(f"    ✗ {symbol} 不在 instruments/all.txt 里")
            continue
        start, end = spans[symbol]
        if start != csv_dates[0] or end != csv_dates[-1]:
            inst_bad += 1
            print(f"    ✗ {symbol} all.txt={start}→{end}，CSV 首末={csv_dates[0]}→{csv_dates[-1]}")
    print(f"[3] instruments 区间：抽 {len(align_files)} 只，不符 {inst_bad} 只；"
          f"all.txt 共 {len(spans)} 条")

    ok = mismatches == 0 and align_bad == 0 and inst_bad == 0
    print(f"结论：{'通过' if ok else '不通过——先别训练'}"
          f"（逐点不符 {mismatches}，错位 {align_bad}，区间不符 {inst_bad}）")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
