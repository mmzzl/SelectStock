"""资金榜预计算：把个股每日主力净额滚动成 3/7/30 交易日的累计榜，前端只读不算。

产出（整文件覆盖，重跑幂等）：
    data/fund_rank/rank.parquet   —— window × code：累计净额、同期成交额、占比、区间涨幅
    data/fund_rank/meta.json      —— as_of、参与日清单、每个窗口的票数、薄日（缺数证据）

口径（都是会被读错的地方，写死在这里）：
- **主力净额 = main_net_inflow = 超大单 + 大单**，与东财个股资金页同尺；单位是元，
  换算「亿」由前端做，服务端不四舍五入；
- 窗口按**交易日**回溯（3/7/30），不是自然日；日历取**分区文件名**（缺一天=少一个文件），
  不从数据里数——从行里数会让缺数的那只票把日历一起缩短，假装完整地进榜；
- 占比 = 窗口净额 ÷ 窗口成交额合计（不是单日占比取平均，那样会让 3 亿成交的小票
  和 300 亿成交的大票同权）；
- 区间涨幅用日 `pct_change` **复利累乘**：日线存的是裸价（§49），首末收盘直接相除会在
  除权日打出假涨跌；
- 窗口内**缺一天不进榜**（含停牌/无成交额这些 `valid=False` 的行）。宁可少一只票，
  也不把「没采到」显示成「没有资金动向」；
- 数据源是 `data/fund_flow/daily/`（东财批量接口，16:30 采集），本脚本只读不算行情。

用法:
    python scripts/build_fund_rank.py
    python scripts/build_fund_rank.py --windows 3,7,30 --limit-codes 200   # 调试
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DAILY_DIR = ROOT / "data" / "fund_flow" / "daily"
OUTPUT_DIR = ROOT / "data" / "fund_rank"
LOG_DIR = ROOT / "logs"

DEFAULT_WINDOWS = (3, 7, 30)
COLUMNS = ["window", "code", "name", "close", "net_inflow", "turnover", "ratio", "ret_pct", "days"]
# 薄日判定同 store.gap_report：中位数票池的 80% 以下。薄日会让所有票的窗口都缺一块，
# 那时榜会集体变短——必须在 meta 里留证据，不然只看到「票数怎么这么少」。
THIN_RATIO = 0.8


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(LOG_DIR / f"fund_rank_{time.strftime('%Y%m%d')}.log", encoding="utf-8"),
        ],
    )


def latest_partitions(daily_dir: Path, days: int) -> list[Path]:
    """最近 `days` 个已落盘分区（按文件名日期排，不要求连续——日历自己会缺）。"""
    paths = [p for p in sorted(Path(daily_dir).glob("*.parquet"))
             if len(p.stem) == 10 and p.stem[4] == "-" and p.stem[7] == "-"]
    return paths[-days:]


def load_frame(paths: list[Path]) -> pd.DataFrame:
    frames = []
    for path in paths:
        df = pd.read_parquet(
            path,
            columns=["trade_date", "code", "name", "close", "pct_change",
                     "amount", "main_net_inflow", "valid"],
        )
        frames.append(df)
    if not frames:
        return pd.DataFrame(columns=["trade_date", "code", "name", "close",
                                     "pct_change", "amount", "main_net_inflow", "valid"])
    return pd.concat(frames, ignore_index=True)


def build_table(frame: pd.DataFrame, windows=DEFAULT_WINDOWS,
                calendar: list[str] | None = None) -> pd.DataFrame:
    """滚动求和成 window × code 的榜表；窗口内不齐的票直接不出现。"""
    if frame is None or frame.empty:
        return pd.DataFrame(columns=COLUMNS)

    df = frame.copy()
    df["code"] = df["code"].astype(str).str.zfill(6)
    # 同日重复行（当日批量 + 次日逐只修复各写一次）按最后一次采集为准，不然求和翻倍
    df = df.drop_duplicates(["trade_date", "code"], keep="last")
    df["trade_date"] = df["trade_date"].astype(str)
    # 日历必须由调用方给（分区文件名）。从行里数日期的话，某只票缺一天只会让日历
    # 跟着缩短，它会假装完整地进榜——「没采到」就变成了「没资金动向」。
    all_days = sorted(calendar) if calendar is not None else sorted(df["trade_date"].unique())

    out = []
    for w in windows:
        if len(all_days) < w:
            continue
        days = all_days[-w:]
        block = df[df["trade_date"].isin(days) & (df["valid"] == True)]  # noqa: E712
        if block.empty:
            continue
        grouped = block.groupby("code", sort=False)
        counts = grouped.size()
        picked = counts[counts == w].index
        block = block[block["code"].isin(picked)]
        if block["main_net_inflow"].isna().any() or block["amount"].isna().any():
            # 净额/成交额缺值的行不进求和：fillna(0) 会把「没采到」算成「没有流向」
            block = block.dropna(subset=["main_net_inflow", "amount"])
            counts = block.groupby("code").size()
            block = block[block["code"].isin(counts[counts == w].index)]
        if block.empty:
            continue

        agg = block.groupby("code", sort=False).agg(
            name=("name", "last"),
            close=("close", "last"),
            net_inflow=("main_net_inflow", "sum"),
            turnover=("amount", "sum"),
        )
        # 复利累乘：(1+p1)(1+p2)...-1，pct_change 单位是百分数
        growth = (1.0 + block["pct_change"].fillna(0.0) / 100.0).groupby(block["code"]).prod() - 1.0
        agg["ret_pct"] = (growth * 100.0).where(agg.index.isin(growth.index))
        agg["ratio"] = agg["net_inflow"] / agg["turnover"].where(agg["turnover"] > 0)
        agg["days"] = w
        agg["window"] = w
        out.append(agg.reset_index())

    if not out:
        return pd.DataFrame(columns=COLUMNS)
    table = pd.concat(out, ignore_index=True)
    return table[COLUMNS].sort_values(["window", "net_inflow"],
                                      ascending=[True, False]).reset_index(drop=True)


def thin_days(paths: list[Path], median_rows: int) -> list[dict]:
    if median_rows <= 0:
        return []
    floor = median_rows * THIN_RATIO
    thin = []
    for path in paths:
        try:
            import pyarrow.parquet as pq
            rows = int(pq.ParquetFile(path).metadata.num_rows)
        except Exception:  # noqa: BLE001  数不出来就当薄日报出来，宁可多一声
            rows = 0
        if rows < floor:
            thin.append({"date": path.stem, "rows": rows})
    return thin


def build(windows, daily_dir: Path, limit_codes: int | None = None) -> dict:
    paths = latest_partitions(daily_dir, max(windows))
    # 交易日日历 = 分区文件名。数据里少了哪天，榜会缩短而不是假装完整。
    calendar = [p.stem for p in paths]
    frame = load_frame(paths)
    if limit_codes:
        keep = sorted(frame["code"].astype(str).unique())[:limit_codes]
        frame = frame[frame["code"].astype(str).str.zfill(6).isin(keep)]
    table = build_table(frame, windows=windows, calendar=calendar)

    row_counts = [len(pd.read_parquet(p, columns=["code"])) for p in paths] or [0]
    median = int(pd.Series(row_counts).median())
    meta = {
        "as_of": paths[-1].stem if paths else None,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "windows": sorted(int(w) for w in table["window"].unique()) if not table.empty else [],
        "days_used": calendar[-max(windows):] if calendar else [],
        "counts_by_window": ({int(w): int((table["window"] == w).sum()) for w in table["window"].unique()}
                             if not table.empty else {}),
        "partition_rows": {p.stem: n for p, n in zip(paths, row_counts)},
        "thin_days": thin_days(paths, median),
    }
    return {"table": table, "meta": meta}


def parse_windows(value: str) -> tuple[int, ...]:
    wins = tuple(int(x) for x in value.split(",") if x.strip())
    if not wins or any(w < 1 or w > 250 for w in wins):
        raise argparse.ArgumentTypeError("--windows 用逗号分隔的 1~250 个交易日，例如 3,7,30")
    return wins


def main() -> None:
    parser = argparse.ArgumentParser(description="资金榜预计算（主力净额 3/7/30 交易日累计）")
    parser.add_argument("--windows", type=parse_windows, default=DEFAULT_WINDOWS)
    parser.add_argument("--limit-codes", type=int, default=None, help="调试用：只算前 N 只票")
    args = parser.parse_args()

    setup_logging()
    if not DAILY_DIR.exists():
        logging.error("没有 %s，请先跑 scripts/fetch_fund_flow.py", DAILY_DIR)
        raise SystemExit(2)

    result = build(args.windows, DAILY_DIR, args.limit_codes)
    table, meta = result["table"], result["meta"]
    if table.empty:
        logging.error("滚动求和后榜为空（分区太少或全被剔），不落盘：meta=%s", meta)
        raise SystemExit(3)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    table.to_parquet(OUTPUT_DIR / "rank.parquet", index=False, compression="zstd")
    (OUTPUT_DIR / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
    logging.info("资金榜完成 asof=%s 票数=%s 窗口=%s 薄日=%s", meta["as_of"], len(table),
                 meta["counts_by_window"], meta["thin_days"] or "无")


if __name__ == "__main__":
    main()
