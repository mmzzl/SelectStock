"""构建可信度（剧本成功率）样本表：逐 (code, date) 用固定屏障向前模拟 N 根。

口径（读数字前必看）：
- **样本日 t 的特征只允许来自 <= t 的 K 线**：直接取 t 行上已经由 build_kline_features 因果算好的
  指标列（ma20/pos60/vol_ratio/vol_z/rsi14/adx14/atr14/lu），本脚本里 `shift(-k)` 只出现在**前向标签**，
  特征侧一根未来 bar 都不碰（回测同期取数入口见 stock_selector/backtest/provider.py 的 window()）。
- 入场价 = close(t)；止损价 = close(t)*(1-stop)、止盈价 = close(t)*(1+take)。
- 逐根前向只用 t+1..t+N 的 low/high：
    同一根先判止损再判止盈；先触者定 label（止损优先，与 scripts/screen_signal_factors.exit_pnl、
    stock_selector/backtest/kernel_strategy 一致）。
- label: 1=先触止盈（成功，期间未触止损）; 0=先触止损（或同根双触，止损优先）; -1=都没触。

用法:
    python scripts/build_conviction_samples.py --codes 50 --out /tmp/conv_smoke.parquet
    python scripts/build_conviction_samples.py                 # 全量
    python scripts/build_conviction_samples.py --horizon 10 --stop 0.05 --take 0.10
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

from stock_selector.conviction.bucket import classify_board  # noqa: E402

KLINE_DIR = ROOT / "data" / "stock_kline"
OUT_FILE = ROOT / "data" / "conviction" / "samples.parquet"
LOG_DIR = ROOT / "logs"

# 建样本需要读的原始列：全是 t 行及以前就定好的因果列
READ_COLUMNS = ["date", "close", "high", "low", "ma20", "pos60",
                "vol_ratio", "vol_z", "rsi14", "adx14", "atr14", "lu"]

# 冻结契约列序 / 类型
SCHEMA = pa.schema([
    ("date", pa.timestamp("ns")),
    ("code", pa.string()),
    ("close", pa.float64()),
    ("board", pa.string()),
    ("bias20", pa.float64()),
    ("pos60", pa.float64()),
    ("vol_ratio", pa.float64()),
    ("vol_z", pa.float64()),
    ("rsi14", pa.float64()),
    ("adx14", pa.float64()),
    ("atr_n", pa.float64()),
    ("lu", pa.int8()),
    ("hit_stop", pa.int8()),
    ("hit_take", pa.int8()),
    ("label", pa.int8()),
    ("fwd_low", pa.float64()),
    ("fwd_high", pa.float64()),
])
SCHEMA_NAMES = [f.name for f in SCHEMA]

logger = logging.getLogger("conviction_samples")


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


def label_barriers(close, high, low, horizon: int, stop: float, take: float):
    """固定屏障逐根前向模拟，返回 (hit_stop, hit_take, label, fwd_low, fwd_high)。

    输入为等长的 numpy 数组（同一只票、按 date 升序）。`shift(-k)` 只在这里出现——它算的是 label，
    不是特征。同一根 low<=止损 与 high>=止盈 同时成立时止损优先（`stop_k <= take_k` 判 hit_stop）。
    """
    close = np.asarray(close, dtype=float)
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    n = close.shape[0]
    stop_price = close * (1.0 - stop)
    take_price = close * (1.0 + take)
    fwd_high = np.full(n, np.nan)
    fwd_low = np.full(n, np.nan)
    sentinel = horizon + 1                      # 大于任何真实 k，表示「窗口内没触」
    stop_k = np.full(n, sentinel)
    take_k = np.full(n, sentinel)
    for k in range(1, horizon + 1):
        hs = np.full(n, np.nan)
        ls = np.full(n, np.nan)
        hs[: n - k] = high[k:]                  # t+k 的 high 对齐到 t
        ls[: n - k] = low[k:]
        fwd_high = np.fmax(fwd_high, hs)
        fwd_low = np.fmin(fwd_low, ls)
        # nan <= x 为 False，停牌/末尾缺价自然不触发
        stop_k = np.where((ls <= stop_price) & (stop_k > k), k, stop_k)
        take_k = np.where((hs >= take_price) & (take_k > k), k, take_k)
    hit_stop = (stop_k <= horizon) & (stop_k <= take_k)     # 同根双触 → 止损优先
    hit_take = (take_k <= horizon) & (take_k < stop_k)
    label = np.full(n, -1, dtype=np.int8)
    label[hit_stop] = 0
    label[hit_take] = 1
    return hit_stop.astype(np.int8), hit_take.astype(np.int8), label, fwd_low, fwd_high


def build_samples_for_frame(df, horizon: int, stop: float, take: float,
                            code: str | None = None, board: str | None = None):
    """单只票 → 样本 DataFrame（列序与契约一致）。只留前向窗口完整、特征可用的行。"""
    d = df.sort_values("date").reset_index(drop=True)
    n = len(d)
    if n <= horizon:
        return pd.DataFrame(columns=SCHEMA_NAMES)
    code = code if code is not None else str(d["code"].iloc[0])
    board = board if board is not None else classify_board(code)

    close = d["close"].astype(float).to_numpy()
    high = d["high"].astype(float).to_numpy()
    low = d["low"].astype(float).to_numpy()
    ma20 = d["ma20"].astype(float).to_numpy()
    atr14 = d["atr14"].astype(float).to_numpy()
    pos60 = d["pos60"].astype(float).to_numpy()
    vol_ratio = d["vol_ratio"].astype(float).to_numpy()
    vol_z = d["vol_z"].astype(float).to_numpy()
    rsi14 = d["rsi14"].astype(float).to_numpy()
    adx14 = d["adx14"].astype(float).to_numpy()
    lu = d["lu"].astype(bool).to_numpy().astype(np.int8)

    hit_stop, hit_take, label, fwd_low, fwd_high = label_barriers(
        close, high, low, horizon, stop, take)

    with np.errstate(divide="ignore", invalid="ignore"):
        bias20 = close / ma20 - 1.0            # 与 stock_selector.features.risk_features 同式
        atr_n = atr14 / close

    out = pd.DataFrame({
        "date": d["date"].to_numpy(),
        "code": np.full(n, code, dtype=object),
        "close": close,
        "board": np.full(n, board, dtype=object),
        "bias20": bias20,
        "pos60": pos60,
        "vol_ratio": vol_ratio,
        "vol_z": vol_z,
        "rsi14": rsi14,
        "adx14": adx14,
        "atr_n": atr_n,
        "lu": lu,
        "hit_stop": hit_stop,
        "hit_take": hit_take,
        "label": label,
        "fwd_low": fwd_low,
        "fwd_high": fwd_high,
    })

    # 前向窗口完整（t+1..t+N 都存在）且核心特征可判
    valid = np.arange(n) <= n - 1 - horizon
    valid &= np.isfinite(close) & (close > 0)
    valid &= np.isfinite(bias20) & np.isfinite(pos60) & np.isfinite(vol_ratio)
    return out.loc[valid].reset_index(drop=True)[SCHEMA_NAMES]


def _read_frame(path: Path) -> pd.DataFrame | None:
    try:
        return pd.read_parquet(path, columns=READ_COLUMNS)
    except Exception as exc:                    # 坏文件不能让整张表起不来
        logger.debug("跳过 %s：%s", path.name, exc)
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description="可信度样本表（屏障前向模拟）")
    ap.add_argument("--horizon", type=int, default=10, help="前向窗口根数")
    ap.add_argument("--stop", type=float, default=0.05, help="止损幅度 close*(1-stop)")
    ap.add_argument("--take", type=float, default=0.10, help="止盈幅度 close*(1+take)")
    ap.add_argument("--codes", type=int, default=0, help="只取前 N 只票（冒烟用）")
    ap.add_argument("--data-dir", default=str(KLINE_DIR), help="K 线目录")
    ap.add_argument("--out", default=str(OUT_FILE), help="输出 parquet 路径")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    setup_logging(args.verbose)
    files = sorted(Path(args.data_dir).glob("*.parquet"))
    if args.codes:
        files = files[: args.codes]
    if not files:
        logger.warning("没有输入文件，未写任何东西")
        return

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()                            # 整表覆盖，重跑幂等

    logger.info("屏障 horizon=%d stop=%.3f take=%.3f，输入 %d 个文件",
                args.horizon, args.stop, args.take, len(files))
    writer = None
    n_rows = 0
    n_files = 0
    labels = [0, 0, 0]                          # [失败, 成功, 中性]
    t0 = time.time()
    try:
        for i, path in enumerate(files, 1):
            raw = _read_frame(path)
            if raw is None or len(raw) <= args.horizon:
                continue
            frame = build_samples_for_frame(raw, args.horizon, args.stop, args.take,
                                            code=path.stem)
            if frame.empty:
                continue
            if writer is None:
                writer = pq.ParquetWriter(str(out), SCHEMA, compression="zstd")
            writer.write_table(pa.Table.from_pandas(frame, schema=SCHEMA,
                                                    preserve_index=False))
            n_rows += len(frame)
            n_files += 1
            lab = frame["label"].to_numpy()
            labels[0] += int((lab == 0).sum())
            labels[1] += int((lab == 1).sum())
            labels[2] += int((lab == -1).sum())
            if i % 200 == 0 or i == len(files):
                logger.info("进度 %d/%d 文件，累计 %d 行 (%.0fs)",
                            i, len(files), n_rows, time.time() - t0)
    finally:
        if writer is not None:
            writer.close()
    if writer is None:
        logger.warning("没有任何样本写出")
        return
    logger.info("已写 %s：%d 文件 / %d 行；成功 %d、失败 %d、中性 %d (%.0fs)",
                out, n_files, n_rows, labels[1], labels[0], labels[2], time.time() - t0)


if __name__ == "__main__":
    main()
