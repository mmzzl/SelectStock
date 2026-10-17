"""批次 D 的新输入：全市场/同板块当日分位 + 市场状态列（README §6「批次 D」口径先写死）。

为什么树要人喂这一列：LightGBM 只能在**行内**比较数值，算不出「今天这个 bias20 在全市场排第几」。
价格绝对水平没有区分度（同一天全场一起涨一起跌），相对强弱才是另一维信息。批 C 的重要度前五名
（bias20/atr_n/pos60/vol_ratio/pct）全是个股自己的位置与波动 ⇒ 9 列同源，榨干了。

三条容易被写错的口径：
- **票池必须与批 C 逐项一致**：同一个 `_read`、同一个 `--to`、同一个 ≥250 根门槛、同一句 ST 判定。
  分位是「在谁中间排」，换池子就是换特征定义，两边就对不上话了。
- **只用 t 日收盘已知的量**：这里的每一列都只由当天各票的当天指标值排序得到，不看任何未来价。
  （标签里减掉的中位数确实要用到 t+H，那是标签定义，见 README；这边是特征侧，一寸都不许越。）
- **分位用 `rank/(n+1)` ∈ (0,1)**，缺值行不参与排（自己留 NaN）：`rank/n` 会把当天最后一个票永远钉在 1.0，
  而 `(n+1)` 分母让分位严格落在开区间，跨票池大小可比。

用法:
    python3 scripts/build_cross_section.py --codes 40 -v        # 冒烟（池子小，分位没有意义，只验管道）
    python3 scripts/build_cross_section.py                      # 全量，写 data/actions/{cross_section,market_state}.parquet
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
for _p in (ROOT, ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from build_action_samples import (DATE_FROM, DATE_TO, KLINE_DIR,  # noqa: E402
                                  MIN_TENURE, _read)  # noqa: E402
from stock_selector.conviction import bucket  # noqa: E402

OUT_DIR = ROOT / "data" / "actions"
XS_FILE = OUT_DIR / "cross_section.parquet"
MKT_FILE = OUT_DIR / "market_state.parquet"

# 个股当日横截面列（键：date, code）
XS_COLUMNS = ["xs_bias20", "xs_pos60", "xs_vol_ratio", "xs_vol_z",
              "xs_board_bias20", "xs_board_pos60"]
# 市场状态列（键：date；每票同值，让模型知道现在是什么行情）
MKT_COLUMNS = ["mkt_up_ratio", "mkt_med_pct", "mkt_vol20", "mkt_n"]

XS_SCHEMA = pa.schema([("date", pa.timestamp("ns")), ("code", pa.string()),
                       *[(c, pa.float32()) for c in XS_COLUMNS]])
MKT_SCHEMA = pa.schema([("date", pa.timestamp("ns")),
                        *[(c, pa.float32()) for c in MKT_COLUMNS]])

logger = logging.getLogger("cross_section")


def pct_rank(values: pd.Series, keys: pd.Series) -> np.ndarray:
    """组内分位 `rank/(n+1)`；NaN 自己不算也不占位。

    分母用组内**有效值个数 + 1**，不是全组行数——否则当天缺数据的票会把所有人的分位压低。
    """
    finite = values.notna()
    n = finite.groupby(keys).transform("sum").to_numpy(dtype=float)
    r = values.groupby(keys).rank(method="average").to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(finite.to_numpy(), r / (n + 1.0), np.nan)
    return out


def market_daily(df: pd.DataFrame) -> pd.DataFrame:
    """按交易日聚合的市场状态：上涨家数占比、涨跌幅中位、该中位的 20 日标准差。

    `mkt_med_pct` 用中位不用均值：全市场均值会被几只 20cm 的票拽走，中位才是「今天持仓普遍什么感受」。
    `mkt_vol20` 只看 ≤t 的 20 行（含当天），是全市场剧烈程度的现算代理，不用任何未来行。
    """
    up = (df["pct"] > 0).astype(float)
    up = up.where(df["pct"].notna())
    g = df.assign(up=up).groupby("day", sort=True)
    out = g.agg(mkt_up_ratio=("up", "mean"), mkt_med_pct=("pct", "median"),
               mkt_n=("pct", "count")).reset_index()
    out["mkt_vol20"] = out["mkt_med_pct"].rolling(20, min_periods=20).std()
    return out


def collect(files: list[Path], min_tenure: int, to_ns: np.datetime64) -> pd.DataFrame:
    """全市场逐票摊平成一张 (day, code, 指标) 长表；池子口径与 `build_action_samples` 同。"""
    frames, t0 = [], time.time()
    for i, path in enumerate(files, 1):
        d = _read(path)
        if d is None or len(d) < 60 or bucket.is_st_name(str(d["name"].iloc[-1] or "")):
            continue
        cut = int(np.searchsorted(d["date"].to_numpy(), to_ns, side="right"))
        d = d.iloc[:cut]
        cl = d["close"].to_numpy(dtype=float)
        have = np.isfinite(cl) & (cl > 0)
        pool = have.copy()
        if min_tenure > 0:
            pool &= np.cumsum(have) >= min_tenure
        ma20 = d["ma20"].to_numpy(dtype=float)
        with np.errstate(divide="ignore", invalid="ignore"):
            bias20 = cl / ma20 - 1.0
        frames.append(pd.DataFrame({
            "day": d["date"].to_numpy().astype("datetime64[D]").astype(np.int32)[pool],
            "code": path.stem,
            "board": np.int8(bucket.board_index(bucket.classify_board(path.stem))),
            "bias20": bias20[pool].astype(np.float32),
            "pos60": d["pos60"].to_numpy(dtype=float)[pool].astype(np.float32),
            "vol_ratio": d["vol_ratio"].to_numpy(dtype=float)[pool].astype(np.float32),
            "vol_z": d["vol_z"].to_numpy(dtype=float)[pool].astype(np.float32),
            "pct": d["pct"].to_numpy(dtype=float)[pool].astype(np.float32),
        }))
        if i % 400 == 0 or i == len(files):
            logger.info("进度 %d/%d 只，累计 %s 行 (%.0fs)", i, len(files),
                        f"{sum(len(f) for f in frames):,}", time.time() - t0)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def build(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """长表 → (个股分位表, 市场状态表)。"""
    day_key = df["day"]
    board_key = df["board"].astype(str) + "_" + day_key.astype(str)
    out = df[["day", "code"]].copy()
    for col, xs in zip(["bias20", "pos60", "vol_ratio", "vol_z"],
                       ["xs_bias20", "xs_pos60", "xs_vol_ratio", "xs_vol_z"]):
        out[xs] = pct_rank(df[col], day_key).astype(np.float32)
    out["xs_board_bias20"] = pct_rank(df["bias20"], board_key).astype(np.float32)
    out["xs_board_pos60"] = pct_rank(df["pos60"], board_key).astype(np.float32)

    mkt = market_daily(df)
    mkt["date"] = pd.to_datetime(mkt["day"].to_numpy(dtype="datetime64[D]"))
    mkt = mkt.drop(columns=["day"])
    out = out.sort_values(["day", "code"], ignore_index=True)
    out["date"] = pd.to_datetime(out["day"].to_numpy(dtype="datetime64[D]"))
    return out[["date", "code", *XS_COLUMNS]], mkt[["date", *MKT_COLUMNS]]


def main() -> None:
    ap = argparse.ArgumentParser(description="横截面分位 + 市场状态（批次 D 的新输入）")
    ap.add_argument("--data-dir", default=str(KLINE_DIR))
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    ap.add_argument("--from", dest="date_from", default=DATE_FROM, help="只算这个日期之后的行")
    ap.add_argument("--to", dest="date_to", default=DATE_TO)
    ap.add_argument("--min-tenure", type=int, default=MIN_TENURE)
    ap.add_argument("--codes", type=int, default=0, help="只取前 N 只票（冒烟，分位无意义）")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    logger.setLevel(logging.DEBUG if args.verbose else logging.INFO)

    files = sorted(p for p in Path(args.data_dir).glob("*.parquet")
                   if bucket.is_stock_code(p.stem))
    if args.codes:
        files = files[: args.codes]
    if not files:
        logger.warning("没有输入文件，未写任何东西")
        return

    from_ns = np.datetime64(pd.Timestamp(args.date_from).to_datetime64()).astype("datetime64[D]").astype(np.int32)
    to_ns = np.datetime64(pd.Timestamp(args.date_to).to_datetime64())
    logger.info("票池 %d 个文件、门槛 ≥%d 根日线、窗口 %s→%s（与批 C 同 `_read`/同池）",
                len(files), args.min_tenure, args.date_from, args.date_to)
    long = collect(files, args.min_tenure, to_ns)
    if long.empty:
        logger.warning("没有任何横截面行")
        return
    long = long[long["day"] >= int(from_ns)]
    logger.info("横截面长表 %s 行 / %d 只 / %d 个交易日", f"{len(long):,}",
                long["code"].nunique(), long["day"].nunique())

    xs, mkt = build(long)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pandas(xs, schema=XS_SCHEMA, preserve_index=False),
                   out_dir / XS_FILE.name, compression="zstd")
    pq.write_table(pa.Table.from_pandas(mkt, schema=MKT_SCHEMA, preserve_index=False),
                   out_dir / MKT_FILE.name, compression="zstd")
    logger.info("已写 %s（%s 行）与 %s（%d 行）", out_dir / XS_FILE.name,
                f"{len(xs):,}", out_dir / MKT_FILE.name, len(mkt))
    logger.info("分位覆盖：xs_bias20 非空 %s、xs_board_pos60 非空 %s；"
                "mkt_vol20 前 19 个交易日按口径留空（不足 20 行不算波动）",
                f"{int(xs['xs_bias20'].notna().sum()):,}",
                f"{int(xs['xs_board_pos60'].notna().sum()):,}")


if __name__ == "__main__":
    main()
