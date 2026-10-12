"""板块热力图预计算：把个股区间涨幅等权聚合成板块格子，前端只读不算。

产出（整文件覆盖，只留最新 asof，重跑幂等）：
    data/sector_heat/board.parquet   —— 格子：kind × 板块 × window 的区间涨幅/涨跌家数/成交额
    data/sector_heat/member.parquet  —— 点开格子的成分股明细

口径：
- window 按「交易日」回溯（3/7/30），不是自然日；涨幅用日 pct 累乘等价于末收盘/基准收盘，
  停牌股按自身最后可得收盘计，全市场窗口边界统一；
- 两套板块都算：sector_stocks（证监会行业）+ bk_stocks（东财概念 BK）。成分数落在
  [min-members, max-members] 之外的伪板块不进热力图（「昨日打二板以上表现」只有 4 只、
  沪深300 那类 3788 只不是「板块」）；
- 成交额：amount 自 2026-05-21 起爬虫不再回填、全是 0，统一按 volume(手)×close×100 估算，
  当期与前期同口径才可比，不要混用 amount。

用法:
    python scripts/build_sector_heat.py
    python scripts/build_sector_heat.py --windows 3,7,30 --asof 2026-10-09   # 窗口是交易日
    python scripts/build_sector_heat.py --kind industry          # 只算行业
    python scripts/build_sector_heat.py --code 600519            # 单只调试
    python scripts/build_sector_heat.py --limit 300              # 调试时限制股票数
"""

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import yaml
from tqdm import tqdm

ROOT = Path(__file__).resolve().parent.parent
KLINE_DIR = ROOT / "data" / "stock_kline"
OUTPUT_DIR = ROOT / "data" / "sector_heat"
LOG_DIR = ROOT / "logs"
CONFIG_PATH = ROOT / "stock_selector" / "config" / "config.yaml"

logger = logging.getLogger("build_sector_heat")

# 每只票只留最近 TAIL 个交易日：30 日窗口要连同前期对比窗口（2×30+1=61 行）一起取，留足余量
TAIL_ROWS = 70
KLINE_COLUMNS = ["date", "name", "close", "pct", "volume", "amount", "lu"]
AMOUNT_FALLBACK = "volume_close"


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    logger.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)
    fh = logging.FileHandler(
        LOG_DIR / f"sector_heat_{time.strftime('%Y%m%d')}.log", encoding="utf-8"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)


def load_mongo_config() -> dict:
    if CONFIG_PATH.exists():
        return (yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}).get("mongodb") or {}
    return {}


def load_boards(kind: str, min_members: int, max_members: int) -> pd.DataFrame:
    """板块成分表：从 Mongo 读两套口径并归一成 (kind, board_code, board_name, stock_code)。"""
    from pymongo import MongoClient

    client = MongoClient(
        host=load_mongo_config().get("host", "127.0.0.1"),
        port=int(load_mongo_config().get("port", 27017)),
        serverSelectionTimeoutMS=5000,
    )
    db = client[load_mongo_config().get("database", "eastmoney_news")]
    frames = []
    try:
        if kind in ("all", "industry"):
            rows = [(str(d.get("sector_code") or "").strip(), d.get("sector_name") or "",
                     str(d.get("stock_code") or "").split(".")[-1].zfill(6))
                    for d in db["sector_stocks"].find({}, {"sector_code": 1, "sector_name": 1, "stock_code": 1})]
            frames.append(pd.DataFrame(rows, columns=["board_code", "board_name", "stock_code"]).assign(kind="industry"))
        if kind in ("all", "concept"):
            rows = [(str(d.get("bk_code") or "").strip(), d.get("bk_name") or "",
                     str(d.get("stock_code") or "").split(".")[-1].zfill(6))
                    for d in db["bk_stocks"].find({}, {"bk_code": 1, "bk_name": 1, "stock_code": 1})]
            frames.append(pd.DataFrame(rows, columns=["board_code", "board_name", "stock_code"]).assign(kind="concept"))
    finally:
        client.close()

    boards = pd.concat(frames, ignore_index=True)
    boards = boards[(boards["board_code"] != "") & (boards["stock_code"].str.len() == 6)]
    boards = boards.drop_duplicates(["kind", "board_code", "stock_code"])
    sizes = boards.groupby(["kind", "board_code"])["stock_code"].transform("count")
    kept = boards[(sizes >= min_members) & (sizes <= max_members)]
    logger.info("板块成分：industry %d 个 / concept %d 个（成分数区间 %d~%d 之外剔除 %d 个）",
                kept[kept["kind"] == "industry"]["board_code"].nunique(),
                kept[kept["kind"] == "concept"]["board_code"].nunique(),
                min_members, max_members,
                boards["board_code"].nunique() - kept["board_code"].nunique())
    return kept.reset_index(drop=True)


def read_stock_frame(path: Path) -> pd.DataFrame | None:
    import pyarrow.parquet as pq

    try:
        available = set(pq.ParquetFile(path).schema_arrow.names)
    except Exception as exc:  # 单个文件坏了不该让整张热力图出不来
        logger.warning("读取失败 %s: %s", path.name, exc)
        return None
    cols = [c for c in KLINE_COLUMNS if c in available]
    df = pd.read_parquet(path, columns=cols)
    for col in ("close", "pct", "volume", "amount"):
        if col not in df.columns:
            df[col] = 0.0
    if "lu" not in df.columns:
        df["lu"] = False
    if "name" not in df.columns:
        df["name"] = ""
    df = df.dropna(subset=["date", "close"]).sort_values("date")
    if df.empty:
        return None
    df = df.tail(TAIL_ROWS).copy()
    df["code"] = path.stem
    df["name"] = df["name"].fillna("").astype(str)
    df["lu"] = df["lu"].fillna(False).astype(bool)
    # amount 断供后是 0，用 volume(手)×close×100 兜底；历史行里两者同量级（比值≈100）
    est = df["volume"].astype(float) * df["close"].astype(float) * 100.0
    df["turnover"] = df["amount"].astype(float).where(df["amount"].astype(float) > 0, est)
    return df[["code", "name", "date", "close", "pct", "lu", "turnover"]]


def collect_klines(codes: list[str] | None, limit: int | None) -> pd.DataFrame:
    files = sorted(KLINE_DIR.glob("*.parquet"))
    if codes:
        wanted = {c.zfill(6) for c in codes}
        files = [f for f in files if f.stem in wanted]
    if limit:
        files = files[:limit]
    frames = []
    for f in tqdm(files, desc="read kline", mininterval=5):
        df = read_stock_frame(f)
        if df is not None:
            frames.append(df)
    if not frames:
        raise SystemExit(f"{KLINE_DIR} 下没有可用日线，热力图无从计算")
    logger.info("读入 %d 只股票的最近 %d 个交易日", len(frames), TAIL_ROWS)
    return pd.concat(frames, ignore_index=True)


def window_stats(klines: pd.DataFrame, sessions: list, window: int) -> pd.DataFrame:
    """每只票在最近 window 个「交易日」的区间涨幅/成交额，基准取窗口前一交易日收盘。

    按交易日历切窗口（不是自然日）：自然日口径下「近 3 天」跨个周末就只剩 2 个交易日，
    周一跑更像 1 天，热力图会莫名跳变。窗口边界全市场统一，停牌股按自身最后可得收盘计。
    """
    if len(sessions) < window * 2 + 1:
        raise SystemExit(f"交易日历只有 {len(sessions)} 天，不够算 {window} 日窗口（需要 {window*2+1} 天）")
    start = sessions[-window]
    prev_start = sessions[-window * 2]
    cur = klines[klines["date"] >= start]
    base = klines[klines["date"] < start].sort_values("date").groupby("code").tail(1)
    if cur.empty or base.empty:
        return pd.DataFrame()
    end = cur.sort_values("date").groupby("code").tail(1).set_index("code")
    base = base.set_index("code")
    prev_turnover = klines[(klines["date"] >= prev_start) & (klines["date"] < start)].groupby("code")["turnover"].sum()
    out = pd.DataFrame({
        "stock_name": end["name"],
        "close": end["close"],
        "pct_last": end["pct"],
        "ret_pct": (end["close"] / base["close"] - 1.0) * 100.0,
        "days": cur.groupby("code").size().reindex(end.index).fillna(0),
        "limit_up": cur.groupby("code")["lu"].any().reindex(end.index).fillna(False),
        "turnover": cur.groupby("code")["turnover"].sum().reindex(end.index).fillna(0.0),
        "turnover_prev": prev_turnover.reindex(end.index).fillna(0.0),
        "end_date": end["date"],
    })
    out = out[out["ret_pct"].notna() & out["close"].notna()]
    out["base_close"] = base["close"]
    out = out[out["base_close"] > 0].drop(columns=["base_close"])
    out["days"] = out["days"].astype(int)
    out["limit_up"] = out["limit_up"].astype(bool)
    out.index.name = "code"
    return out.reset_index().rename(columns={"code": "stock_code"})


def aggregate_boards(boards: pd.DataFrame, stats: pd.DataFrame, window: int, asof: pd.Timestamp) -> tuple[pd.DataFrame, pd.DataFrame]:
    """等权聚合：板块涨幅 = 成分区间涨幅均值（不做市值加权，热力图看的是普涨普跌）。"""
    member = boards.merge(stats, on="stock_code", how="inner")
    if member.empty:
        return pd.DataFrame(), pd.DataFrame()
    member["window"] = window
    member["asof"] = asof
    member["up"] = member["ret_pct"] > 0
    member["down"] = member["ret_pct"] < 0

    grouped = member.groupby(["kind", "board_code"], as_index=False).agg(
        board_name=("board_name", "first"),
        stock_count=("stock_code", "size"),
        ret_pct=("ret_pct", "mean"),
        median_ret_pct=("ret_pct", "median"),
        up_count=("up", "sum"),
        down_count=("down", "sum"),
        limit_up_count=("limit_up", "sum"),
        turnover=("turnover", "sum"),
        turnover_prev=("turnover_prev", "sum"),
    )
    leader_idx = member.groupby(["kind", "board_code"])["ret_pct"].idxmax()
    lagger_idx = member.groupby(["kind", "board_code"])["ret_pct"].idxmin()
    leaders = member.loc[leader_idx, ["kind", "board_code", "stock_code", "stock_name", "ret_pct"]].set_index(["kind", "board_code"])
    lagger = member.loc[lagger_idx, ["kind", "board_code", "stock_code", "stock_name", "ret_pct"]].set_index(["kind", "board_code"])
    grouped = grouped.merge(
        leaders.rename(columns={"stock_code": "leader_code", "stock_name": "leader_name", "ret_pct": "leader_ret"}),
        on=["kind", "board_code"], how="left",
    ).merge(
        lagger.rename(columns={"stock_code": "lagger_code", "stock_name": "lagger_name", "ret_pct": "lagger_ret"}),
        on=["kind", "board_code"], how="left",
    )
    total = boards.groupby(["kind", "board_code"])["stock_code"].nunique().rename("member_count")
    grouped = grouped.set_index(["kind", "board_code"]).join(total, how="left").reset_index()
    grouped["flat_count"] = grouped["stock_count"] - grouped["up_count"] - grouped["down_count"]
    for col in ("stock_count", "up_count", "down_count", "flat_count", "limit_up_count", "member_count"):
        grouped[col] = grouped[col].astype(int)
    grouped["turnover_ratio"] = (grouped["turnover"] / grouped["turnover_prev"].where(grouped["turnover_prev"] > 0)).round(4)
    grouped["asof"] = asof
    grouped["window"] = window
    grouped["ret_pct"] = grouped["ret_pct"].round(4)
    member["ret_pct"] = member["ret_pct"].round(4)
    return grouped, member.drop(columns=["up", "down"])


def build(windows: list[int], kind: str, asof_arg: str | None, min_members: int,
          max_members: int, codes: list[str] | None, limit: int | None, out_dir: Path) -> dict:
    klines = collect_klines(codes, limit)
    asof = pd.Timestamp(asof_arg) if asof_arg else klines["date"].max()
    sessions = sorted(d for d in klines["date"].unique() if d <= asof)
    if not sessions or sessions[-1] != asof:
        raise SystemExit(f"--asof {asof.date()} 当天没有日线，请取交易日（数据最新 {sessions[-1].date()}）")
    logger.info("锚定交易日 asof=%s，交易日历 %d 天，窗口 %s 交易日，口径 %s",
                asof.date(), len(sessions), windows, kind)

    boards = load_boards(kind, min_members, max_members)
    board_rows, member_rows = [], []
    for window in windows:
        if window * 2 + 1 > TAIL_ROWS:
            logger.warning("窗口 %d 日超出每票保留的 %d 行，前期成交额会不完整", window, TAIL_ROWS)
        stats = window_stats(klines, sessions, window)
        if stats.empty:
            logger.warning("窗口 %d 日没有任何个股可算，跳过", window)
            continue
        grouped, member = aggregate_boards(boards, stats, window, asof)
        logger.info("窗口 %2d 日：个股 %d 只 → 板块格子 %d 个（industry %d / concept %d）",
                    window, len(stats), len(grouped),
                    int((grouped["kind"] == "industry").sum()), int((grouped["kind"] == "concept").sum()))
        board_rows.append(grouped)
        member_rows.append(member)
    if not board_rows:
        raise SystemExit("没有任何板块格子产出，检查 Mongo 板块成分与 --asof")

    board_df = pd.concat(board_rows, ignore_index=True).sort_values(
        ["kind", "window", "ret_pct"], ascending=[True, True, False]
    ).reset_index(drop=True)
    member_df = pd.concat(member_rows, ignore_index=True).sort_values(
        ["kind", "window", "board_code", "ret_pct"], ascending=[True, True, True, False]
    ).reset_index(drop=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    for name, df in (("board", board_df), ("member", member_df)):
        path = out_dir / f"{name}.parquet"
        df.to_parquet(path, compression="zstd", index=False)
        logger.info("写出 %s（%d 行）", path, len(df))
    return {"asof": str(asof.date()), "boards": len(board_df), "members": len(member_df)}


def parse_windows(value: str) -> list[int]:
    out = sorted({int(x) for x in value.split(",") if x.strip()})
    if not out or any(w < 1 or w > 250 for w in out):
        raise argparse.ArgumentTypeError("--windows 用逗号分隔的 1~250 个交易日，例如 3,7,30")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="板块热力图预计算（行业+概念 × 3/7/30 日）")
    parser.add_argument("--windows", type=parse_windows, default=[3, 7, 30], help="交易日窗口，逗号分隔")
    parser.add_argument("--asof", default=None, help="锚定交易日 YYYY-MM-DD，默认取全市场最新日线日期")
    parser.add_argument("--kind", choices=["all", "industry", "concept"], default="all", help="板块口径")
    parser.add_argument("--min-members", type=int, default=5, help="成分数下限，滤掉凑数板块")
    parser.add_argument("--max-members", type=int, default=900, help="成分数上限，滤掉宽基指数类伪板块")
    parser.add_argument("--code", nargs="*", default=None, help="只算这些代码（调试）")
    parser.add_argument("--limit", type=int, default=None, help="只读前 N 只股票（调试）")
    parser.add_argument("--out-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()

    setup_logging()
    started = time.time()
    out = build(args.windows, args.kind, args.asof, args.min_members, args.max_members,
                args.code, args.limit, args.out_dir)
    logger.info("完成：asof=%s 格子 %d 行 / 成分 %d 行，耗时 %.1fs",
                out["asof"], out["boards"], out["members"], time.time() - started)


if __name__ == "__main__":
    main()
