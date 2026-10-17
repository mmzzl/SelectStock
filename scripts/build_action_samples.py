"""构建「持仓动作」决策样本表：每个持仓决策点一行，标签是「再拿 H 天」的前向收益。

口径预注册在 `stock_selector_api/README.md` §6「持仓动作模型批次 C」——**先看那段再看这里**。
一句话原理：卖出、加仓、提前止盈三个动作，扣完成本后都只是
`r_H =（从今天起再拿 H 天的卖价）/（今天卖掉的价） − 1` 这一个量的单调函数，
所以这里只存 `r_H`，不存三个标签——训一个回归 `r_H` 的模型就够，别把同一件事做三遍。

三条容易被写错的地方：
- **标签用未来、特征只用 ≤t**：`r_H` 取的是决策日之后的行情，特征全部来自决策日那一行上
  已由 `build_kline_features.py` 因果算好的列。相对成本的浮动盈亏 `pnl_pp` 是**特征**
  （要学的正是「浮亏 X% 之后继续拿是不是更好」），绝不进标签——沉没成本不该影响当下的决策。
- **撮合判定与两条链路同源**（`backtest/executability.py`）：入场日贴着涨停线 ⇒ 这个 episode
  不成立；决策日一字板/停牌 ⇒ 当天根本没有「卖掉」这个选项，这行不产出；到期日卖不掉 ⇒ 最多顺移
  `REPAIR_MAX_DAYS` 行，顺移不到判「不可算」（留 NaN），不当 0。
- **断档（#144）必须显式挡**：episode 用到的相邻行间隔超过 `--max-gap-days` 个自然日就整段丢弃。
  301212/301353 那批票「往后数 10 行」跨了 100 个交易日，不挡的话模型学的是「十天后」、
  数据给的是「三个月后」。

用法:
    python3 scripts/build_action_samples.py --codes 30 --out /tmp/act_smoke.parquet -v
    python3 scripts/build_action_samples.py                    # 全量（默认 2015-06-01→2021-12-31）
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_selector.backtest import executability  # noqa: E402
from stock_selector.conviction import bucket  # noqa: E402
from stock_selector.features.base_columns import board_limit_threshold  # noqa: E402

KLINE_DIR = ROOT / "data" / "stock_kline"
OUT_FILE = ROOT / "data" / "actions" / "decision_samples.parquet"
LOG_DIR = ROOT / "logs" / "actions"

DATE_FROM = "2015-06-01"
DATE_TO = "2021-12-31"
HMAX = 10                    # 决策行出到持仓第 10 天（T+1 ⇒ 没有 h=0）
STRIDE = 12                  # 每 12 个交易日抽一个入场日：> HMAX ⇒ 相邻 episode 不共用前向 bar
MIN_TENURE = 250             # 与 v1/B 同一个票池门槛
AMOUNT_WINDOW = 20           # 流动性窗口，与 profit_scan 一致
HORIZONS = (5, 10)           # 对比窗口：再拿 5 天 / 再拿 10 天

# 决策样本的列契约。float32 而不是 float64：全量约 300 万行，多留一倍内存没必要
SCHEMA = pa.schema([
    ("date", pa.timestamp("ns")),             # 决策日 j
    ("code", pa.string()),
    ("board", pa.string()),
    ("entry_date", pa.timestamp("ns")),
    ("ep_idx", pa.int32()),                   # 该票第几个 episode（评测按它去重：同一仓位只算第一次触发）
    ("hold_days", pa.int16()),
    ("buyable", pa.int8()),                   # 决策日按收盘加得进仓吗（贴着涨停线就加不进）
    ("pnl_pp", pa.float32()),                 # 相对入场价的浮动盈亏（pp）——特征，不是标签
    ("pnl_atr", pa.float32()),                # 同一盈亏按当日 ATR 归一（跌 8% 在 3 倍波动的票上不算事）
    ("bias20", pa.float32()),
    ("pos60", pa.float32()),
    ("vol_ratio", pa.float32()),
    ("vol_z", pa.float32()),
    ("rsi14", pa.float32()),
    ("adx14", pa.float32()),
    ("atr_n", pa.float32()),
    ("pct", pa.float32()),
    ("dd_hi20", pa.float32()),                # 距近 20 日最高价的回撤（pp）：向下摊低的核心状态
    ("lu", pa.int8()),
    ("r5", pa.float32()),                     # 再拿 5 天的前向收益（小数）；NaN=判不了
    ("r10", pa.float32()),
])
SCHEMA_NAMES = [f.name for f in SCHEMA]
READ_COLUMNS = ["date", "name", "open", "high", "low", "close", "pct", "last_close",
                "ma20", "pos60", "vol_ratio", "vol_z", "rsi14", "adx14", "atr14", "lu",
                "amount"]

logger = logging.getLogger("action_samples")


def setup_logging(verbose: bool = False) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in list(logger.handlers):
        logger.removeHandler(h)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)


def change_pct(close, pct_col, last_close):
    """当日涨跌幅（%）：日线自带优先，缺了按昨收折算，都没有留 NaN。

    与 `executability.day_change_pct` 同一优先级（那边逐行、这边整只票向量化）。
    NaN 是「无从判定」，调用方按可成交放行但要计数——静默当 0 会让人以为校验真的跑过。
    """
    out = np.asarray(pct_col, dtype=float).copy()
    bad = ~np.isfinite(out)
    with np.errstate(divide="ignore", invalid="ignore"):
        calc = (np.asarray(close, dtype=float) / np.asarray(last_close, dtype=float) - 1.0) * 100.0
    out[bad] = calc[bad]
    return out


def trailing_mean(col: np.ndarray, days: int) -> np.ndarray:
    """近 `days` 行（含当天）均值，NaN 当缺数跳过；整窗没有有效值 → NaN。与 profit_scan 同形。"""
    n = col.size
    v = np.where(np.isfinite(col), col, 0.0)
    k = np.isfinite(col).astype(float)
    cs_v, cs_k = np.cumsum(v), np.cumsum(k)
    start = np.maximum(np.arange(n) - days + 1, 0)
    head_v = np.where(start > 0, cs_v[start - 1], 0.0)
    head_k = np.where(start > 0, cs_k[start - 1], 0.0)
    win_v, win_k = cs_v - head_v, cs_k - head_k
    return np.where(win_k > 0, win_v / np.maximum(win_k, 1), np.nan)


def run_end(bad: np.ndarray, n: int) -> np.ndarray:
    """`run_end[i]` = 从第 i 行起、中间没有断档能走到的最后一行下标。"""
    bi = np.flatnonzero(bad)
    k = np.searchsorted(bi, np.arange(n), side="left")
    has = k < bi.size
    out = np.full(n, n - 1, dtype=np.int32)
    out[has] = bi[k[has]]
    return out


def first_sellable_after(avail: np.ndarray, offset: int, span: int) -> np.ndarray:
    """每行往后第 offset..offset+span 行里第一个「卖得掉」的行下标，没有给 −1。

    这就是顺移规则：到期日一字板/停牌 ⇒ 等后面第一个有对手盘的日子，最多等 `span` 行。
    """
    n = avail.size
    out = np.full(n, -1, dtype=np.int32)
    done = np.zeros(n, dtype=bool)
    for k in range(offset, min(offset + span, n - 1) + 1):
        src = np.arange(n - k)
        dst = src + k
        take = avail[dst] & ~done[src]
        if not take.any():
            continue
        hit = src[take]
        out[hit] = dst[take]
        done[hit] = True
    return out


def build_for_stock(d: pd.DataFrame, code: str, horizons, stride: int, min_tenure: int,
                    min_amount: float, max_gap_days: int, date_from: str, cut: int):
    """单只票 → 决策行 DataFrame + 计数。`cut` 是截到的行尾（超出 `--to` 的行一栏都不碰）。"""
    d = d.iloc[:cut].reset_index(drop=True)
    n = len(d)
    cnt = {"episodes": 0, "drop_tail": 0, "drop_gap": 0, "rows": 0, "row_no_sell": 0,
           "pct_unknown": 0, "r_unfilled": 0}
    need = HMAX + max(horizons) + executability.REPAIR_MAX_DAYS
    if n <= need:
        return pd.DataFrame(columns=SCHEMA_NAMES), cnt

    dd = d["date"].to_numpy()
    cl = d["close"].to_numpy(dtype=float)
    hi = d["high"].to_numpy(dtype=float)
    lo = d["low"].to_numpy(dtype=float)
    have = np.isfinite(cl) & (cl > 0)
    pool = have.copy()
    if min_tenure > 0:
        pool &= np.cumsum(have) >= min_tenure
    if min_amount > 0:
        pool &= trailing_mean(d["amount"].to_numpy(dtype=float), AMOUNT_WINDOW) >= min_amount

    pct = change_pct(cl, d["pct"].to_numpy(dtype=float), d["last_close"].to_numpy(dtype=float))
    unknown = ~np.isfinite(pct)
    # 贴着涨停线就当买不进；一字与否不改变这条结论（与 profit_scan.entry_gate 合口径）
    buy_ok = pool & (np.where(unknown, -np.inf, pct)
                     < board_limit_threshold(code) - executability.LIMIT_TOL_PP)
    sell_ok = pool & np.isfinite(hi) & np.isfinite(lo) & (hi != lo)

    bad = np.zeros(max(n - 1, 0), dtype=bool)
    if n > 1:
        bad[:] = (dd[1:] - dd[:-1]) > np.timedelta64(max_gap_days, "D")
    reach = run_end(bad, n)

    seq = np.cumsum(pool) - 1                       # 池内第几根：入场日按它抽样，跨停牌不串位
    cand = np.flatnonzero(buy_ok & (seq % stride == 0))
    cand = cand[dd[cand] >= np.datetime64(date_from)]
    after_tail = cand[cand + need < n]
    cnt["drop_tail"] = int(cand.size - after_tail.size)
    keep = after_tail[reach[after_tail] >= after_tail + need]
    cnt["drop_gap"] = int(after_tail.size - keep.size)
    cnt["episodes"] = int(keep.size)
    if keep.size == 0:
        return pd.DataFrame(columns=SCHEMA_NAMES), cnt

    js = keep[:, None] + np.arange(1, HMAX + 1)[None, :]
    flat = js.ravel()
    sellable = sell_ok[flat]
    cnt["row_no_sell"] = int((~sellable).sum())

    r_cols = {}
    for h in horizons:
        idx = first_sellable_after(sell_ok, h, executability.REPAIR_MAX_DAYS)[flat]
        with np.errstate(divide="ignore", invalid="ignore"):
            okf = idx >= 0
            r = np.full(flat.size, np.nan, dtype=float)
            pos = np.flatnonzero(okf)
            r[pos] = cl[idx[pos]] / cl[flat[pos]] - 1.0
        r_cols[h] = r
        cnt["r_unfilled"] += int((~okf).sum())

    entry_cl = np.repeat(cl[keep], HMAX)
    with np.errstate(divide="ignore", invalid="ignore"):
        pnl_pp = (cl[flat] / entry_cl - 1.0) * 100.0
        atr_n = d["atr14"].to_numpy(dtype=float)[flat] / cl[flat]
        pnl_atr = np.where(atr_n > 1e-6, pnl_pp / np.maximum(atr_n * 100.0, 1e-9), np.nan)
        bias20 = cl[flat] / d["ma20"].to_numpy(dtype=float)[flat] - 1.0
        hi20 = d["high"].rolling(20, min_periods=20).max().to_numpy(dtype=float)[flat]
        dd_hi20 = (cl[flat] / hi20 - 1.0) * 100.0

    out = pd.DataFrame({
        "date": dd[flat],
        "code": np.full(flat.size, code, dtype=object),
        "board": np.full(flat.size, bucket.classify_board(code), dtype=object),
        "entry_date": np.repeat(dd[keep], HMAX),
        "ep_idx": np.repeat(np.arange(keep.size, dtype=np.int32), HMAX),
        "hold_days": np.tile(np.arange(1, HMAX + 1, dtype=np.int16), keep.size),
        "buyable": buy_ok[flat].astype(np.int8),
        "pnl_pp": pnl_pp.astype(np.float32),
        "pnl_atr": pnl_atr.astype(np.float32),
        "bias20": bias20.astype(np.float32),
        "pos60": d["pos60"].to_numpy(dtype=float)[flat].astype(np.float32),
        "vol_ratio": d["vol_ratio"].to_numpy(dtype=float)[flat].astype(np.float32),
        "vol_z": d["vol_z"].to_numpy(dtype=float)[flat].astype(np.float32),
        "rsi14": d["rsi14"].to_numpy(dtype=float)[flat].astype(np.float32),
        "adx14": d["adx14"].to_numpy(dtype=float)[flat].astype(np.float32),
        "atr_n": (atr_n * 100.0).astype(np.float32),
        "pct": pct[flat].astype(np.float32),
        "dd_hi20": dd_hi20.astype(np.float32),
        "lu": d["lu"].to_numpy(dtype=bool)[flat].astype(np.int8),
        "r5": r_cols[5] if 5 in horizons else np.full(flat.size, np.nan),
        "r10": r_cols[10] if 10 in horizons else np.full(flat.size, np.nan),
    })
    out = out.loc[sellable].reset_index(drop=True)
    cnt["rows"] = int(len(out))
    cnt["pct_unknown"] = int(unknown[flat][sellable].sum())
    return out[SCHEMA_NAMES], cnt


def _read(path: Path) -> pd.DataFrame | None:
    try:
        d = pd.read_parquet(path, columns=READ_COLUMNS).sort_values("date")
        d["date"] = pd.to_datetime(d["date"])
        return d.reset_index(drop=True)
    except Exception as exc:
        logger.debug("跳过 %s：%s", path.name, exc)
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description="持仓动作决策样本（标签 = 再拿 H 天的前向收益）")
    ap.add_argument("--data-dir", default=str(KLINE_DIR))
    ap.add_argument("--out", default=str(OUT_FILE))
    ap.add_argument("--horizons", default=",".join(str(h) for h in HORIZONS),
                    help=f"对比窗口（交易日），逗号分隔，最大 {HMAX}")
    ap.add_argument("--stride", type=int, default=STRIDE, help="每几个交易日抽一个入场日")
    ap.add_argument("--min-tenure", type=int, default=MIN_TENURE)
    ap.add_argument("--min-amount", type=float, default=0.0, help="默认 0：与 v1/B 同池")
    ap.add_argument("--max-gap-days", type=int, default=15, help="相邻行隔这么多自然日算断档")
    ap.add_argument("--from", dest="date_from", default=DATE_FROM)
    ap.add_argument("--to", dest="date_to", default=DATE_TO)
    ap.add_argument("--codes", type=int, default=0, help="只取前 N 只票（冒烟）")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    setup_logging(args.verbose)
    horizons = tuple(sorted({int(x) for x in str(args.horizons).split(",") if x.strip()}))
    if not horizons or not set(horizons) <= set(HORIZONS):
        raise SystemExit(f"--horizons 只能是 {HORIZONS} 的子集（表里就这两列标签），收到 {horizons}")

    files = sorted(p for p in Path(args.data_dir).glob("*.parquet")
                   if bucket.is_stock_code(p.stem))
    if args.codes:
        files = files[: args.codes]
    if not files:
        logger.warning("没有输入文件，未写任何东西")
        return

    need = HMAX + max(horizons) + executability.REPAIR_MAX_DAYS
    to_ns = np.datetime64(pd.Timestamp(args.date_to).to_datetime64())
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()                        # 整表覆盖，重跑幂等

    logger.info("入场抽样 stride=%d、决策 h=1..%d、对比窗口 %s、断档上限 %d 自然日、"
                "每条决策尾部留空 %d 行（--to 之后的行情一栏都不碰）",
                args.stride, HMAX, horizons, args.max_gap_days, need)
    writer = None
    tot = dict.fromkeys(("files", "episodes", "drop_tail", "drop_gap", "rows",
                         "row_no_sell", "pct_unknown", "r_unfilled"), 0)
    t0, last_date = time.time(), None
    try:
        for i, path in enumerate(files, 1):
            d = _read(path)
            if d is None or len(d) < 60 or bucket.is_st_name(str(d["name"].iloc[-1] or "")):
                continue
            cut = int(np.searchsorted(d["date"].to_numpy(), to_ns, side="right"))
            frame, cnt = build_for_stock(d.drop(columns=["name"]), path.stem, horizons,
                                         args.stride, args.min_tenure, args.min_amount,
                                         args.max_gap_days, args.date_from, cut)
            if frame.empty:
                continue
            if writer is None:
                writer = pq.ParquetWriter(str(out_path), SCHEMA, compression="zstd")
            writer.write_table(pa.Table.from_pandas(frame, schema=SCHEMA, preserve_index=False))
            for k, v in cnt.items():
                tot[k] += v
            tot["files"] += 1
            last_date = str(pd.Timestamp(frame["date"].iloc[-1]).date())
            if i % 300 == 0 or i == len(files):
                logger.info("进度 %d/%d 只，累计 %s 行 (%.0fs)",
                            i, len(files), f"{tot['rows']:,}", time.time() - t0)
    finally:
        if writer is not None:
            writer.close()
    if writer is None:
        logger.warning("没有任何样本行")
        return
    logger.info("已写 %s：%d 只票 / episode %s / 决策行 %s，最后一个决策日 %s（%.0fs）",
                out_path, tot["files"], f"{tot['episodes']:,}", f"{tot['rows']:,}",
                last_date, time.time() - t0)
    logger.info("丢弃口径：断档 episode %s、尾部不足 %s、决策日卖不掉/停牌而丢的行 %s、"
                "顺移后仍取不到卖价的标签 %s 个、涨跌幅无从判定 %s 行",
                f"{tot['drop_gap']:,}", f"{tot['drop_tail']:,}", f"{tot['row_no_sell']:,}",
                f"{tot['r_unfilled']:,}", f"{tot['pct_unknown']:,}")


if __name__ == "__main__":
    main()
