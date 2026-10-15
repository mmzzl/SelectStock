"""构建可信度（剧本成功率）表：把样本按 (板块 × bias20 × pos60 × vol_ratio 三分位) 落格，算 Beta 后验。

**已被 scripts/train_conviction_model.py 取代（2026-10-15 审计）**，保留只作对照：
- 表里的 n/95%CI 把逐日滑动窗口当独立样本，而相邻样本共用 9/10 根前向 bar——CI 实窄 3.16 倍
  （stride-10 重采样后可信格 73→28），线上文案印的那个区间是不可核对的；
- 2006→2026 一锅后验，格子止盈率前后段中位漂移 5.55pp、排名相关 0.50，「高/中/低」混进了行情时代；
- 场内基金以前被 classify_board 并进主板（现单成一类），曾经把主板格子拉偏 2pp。

为什么要 era-consistent 分箱（避免分布泄漏）：
- 分箱只用 `--edges-end`（默认 2020-12-31）**及以前**的样本算 1/3、2/3 分位，再把这组边缘套用到全历史。
  若用全样本（含未来）分位去切历史，2016 年的样本会按 2026 年的波动分布落箱——未来信息从箱边界渗进
  历史特征，就是分布泄漏。边缘固定后，同一格在时间上口径一致，跨时代才可比。
- 边缘随表一起落盘（`*_e0/*_e1` 列），线上插件读回来用同一套 `stock_selector.conviction.bucket.assign_bin`
  落箱，保证「线上今天落哪格」和「表里那格怎么统计的」是同一件事。

后验：Beta(1,1) 先验的止盈率均值 + 95%CI（复用 build_judgement_ledger.beta_interval）。
- 默认把中性（窗口内既没触止盈也没触止损）排除在分母外；`--include-neutral` 可把中性并入分母。
- `credible` = CI 宽度 <= 2.0pp 且样本量 n >= 300（与 build_judgement_ledger 的 MIN_POSTERIOR_N 一致）。
- n_success/n_failure/n_neutral 始终写出，中性不会被悄悄丢掉。

用法:
    python scripts/build_conviction_table.py --samples data/conviction/samples.parquet
    python scripts/build_conviction_table.py --include-neutral --edges-end 2018-12-31
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.build_judgement_ledger import beta_interval  # noqa: E402
from stock_selector.conviction import bucket  # noqa: E402

SAMPLES_FILE = ROOT / "data" / "conviction" / "samples.parquet"
OUT_FILE = ROOT / "data" / "conviction" / "conviction_table.parquet"
LOG_DIR = ROOT / "logs"

MIN_TRAIN_N = 10_000        # 训练区间样本太少就退回全样本分位，宁可用全量也别用几十条切
MIN_POSTERIOR_N = 300      # 与 build_judgement_ledger.MIN_POSTERIOR_N 对齐
CREDIBLE_WIDTH_PP = 2.0

# 等级：相对全局基准（base_rate = 全样本止盈率 成功/(成功+失败)），不是绝对百分比。
# 剧本成功率的天然量级就在 30% 上下（+10% 止盈 / -5% 止损 / 10 日窗口），用绝对阈值
# （如 >=60）会把 81 个格子全判成「低」，等级彻底失去信息量。相对基准是「同类行情里
# 相对更容易达标」，对行情漂移也更稳。
BENCH_HIGH_RATIO = 1.05
BENCH_LOW_RATIO = 0.95

TABLE_COLUMNS = [
    "board", "bias_bin", "pos_bin", "vol_bin",
    "n", "n_success", "n_failure", "n_neutral",
    "hit_rate", "base_rate", "level",
    "ci_low", "ci_high", "ci_width_pp", "credible",
    "horizon", "stop", "take",
    "bias_e0", "bias_e1", "pos_e0", "pos_e1", "vol_e0", "vol_e1",
]

logger = logging.getLogger("conviction_table")


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


def compute_edges(samples: pd.DataFrame, edges_end: str) -> dict:
    """在 [最早, edges_end] 的训练区间上取 (bias20, pos60, vol_ratio) 的 1/3、2/3 分位作为边缘。

    只看训练区间是为了不让未来分布渗出；训练样本太少时退回全样本分位并记 warn（此时口径仍是
    「同一组边缘套全历史」，只是边缘不再严格 era-consistent）。
    """
    dates = pd.to_datetime(samples["date"])
    train = samples.loc[dates <= pd.Timestamp(edges_end)]
    if len(train) < MIN_TRAIN_N:
        logger.warning("训练区间(%s 及以前)样本仅 %d 条，退回全样本分位", edges_end, len(train))
        train = samples
    edges: dict[str, tuple[float, float]] = {}
    for feat in bucket.BIN_FEATURES:
        vals = pd.to_numeric(train[feat], errors="coerce").dropna()
        q = vals.quantile([1.0 / 3.0, 2.0 / 3.0]).to_numpy(dtype=float)
        edges[feat] = (float(q[0]), float(q[1]))
        logger.info("分箱边缘 %-9s 1/3=%.5f  2/3=%.5f（训练 %d 条）", feat, q[0], q[1], len(vals))
    return edges


def add_bins(samples: pd.DataFrame, edges: dict) -> pd.DataFrame:
    """补三列箱号；返回同一张表。

    全量样本 ~10.7M 行、机器可用内存常常只有 1~2G，这里**刻意不 copy**（原位加列），
    否则 samples 与副本同时在内存里，峰值直接翻倍。调用方当作原地修改即可。
    """
    for feat in bucket.BIN_FEATURES:
        samples[bucket.BIN_COLUMNS[feat]] = bucket.bin_values(
            samples[feat].to_numpy(), edges[feat])
    return samples


def level_of(hit_rate: float, base_rate: float) -> str:
    """相对全局基准的高/中/低。

    剧本成功率天然在 ~30% 量级，绝对阈值（>=60 之类）会把所有格子判成「低」。改用相对基准：
    比全局止盈率明显高 = 高，明显低 = 低，其余中。基准为 0（无样本）时一律返回「低」，
    避免拿不到基准还硬标一个档位。
    """
    if base_rate <= 0:
        return "低"
    if hit_rate >= base_rate * BENCH_HIGH_RATIO:
        return "高"
    if hit_rate <= base_rate * BENCH_LOW_RATIO:
        return "低"
    return "中"


def summarize(binned: pd.DataFrame, edges: dict, horizon: int, stop: float, take: float,
              include_neutral: bool) -> pd.DataFrame:
    """按 (board, bias_bin, pos_bin, vol_bin) 聚合后验。"""
    usable = binned.loc[(binned[list(bucket.BIN_COLUMNS.values())] >= 0).all(axis=1)]
    dropped = len(binned) - len(usable)
    if dropped:
        logger.info("丢弃 %d 行（分箱特征缺失，落 -1 箱）", dropped)
    # 全局基准：所有可用样本的止盈率（中性是否进分母与本次口径一致）。
    lab_all = usable["label"].to_numpy()
    succ_all = int((lab_all == 1).sum())
    fail_all = int((lab_all == 0).sum())
    denom_all = len(lab_all) if include_neutral else succ_all + fail_all
    base_rate = (succ_all / denom_all) if denom_all else 0.0
    logger.info("全局基准 base_rate=%.2f%%（成功 %d / 分母 %d，含中性=%s）",
                base_rate * 100, succ_all, denom_all, include_neutral)
    rows = []
    keys = ["board", "bias_bin", "pos_bin", "vol_bin"]
    for (board, b0, b1, b2), grp in usable.groupby(keys, observed=True):
        label = grp["label"].to_numpy()
        n = len(label)
        n_success = int((label == 1).sum())
        n_failure = int((label == 0).sum())
        n_neutral = int((label == -1).sum())
        total = n if include_neutral else n_success + n_failure
        mean, lo, hi = beta_interval(n_success, total)
        width_pp = (hi - lo) * 100.0
        rows.append({
            "board": str(board),
            "bias_bin": int(b0), "pos_bin": int(b1), "vol_bin": int(b2),
            "n": n, "n_success": n_success, "n_failure": n_failure, "n_neutral": n_neutral,
            "hit_rate": round(float(mean), 6),
            "base_rate": round(float(base_rate), 6),
            "level": level_of(float(mean), base_rate),
            "ci_low": round(float(lo), 6), "ci_high": round(float(hi), 6),
            "ci_width_pp": round(float(width_pp), 4),
            "credible": bool(width_pp <= CREDIBLE_WIDTH_PP and n >= MIN_POSTERIOR_N),
            "horizon": int(horizon), "stop": float(stop), "take": float(take),
            "bias_e0": edges["bias20"][0], "bias_e1": edges["bias20"][1],
            "pos_e0": edges["pos60"][0], "pos_e1": edges["pos60"][1],
            "vol_e0": edges["vol_ratio"][0], "vol_e1": edges["vol_ratio"][1],
        })
    out = pd.DataFrame(rows, columns=TABLE_COLUMNS)
    if not out.empty:
        out = out.sort_values(["board", "bias_bin", "pos_bin", "vol_bin"]).reset_index(drop=True)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="可信度表（剧本成功率后验）")
    ap.add_argument("--samples", default=str(SAMPLES_FILE))
    ap.add_argument("--out", default=str(OUT_FILE))
    ap.add_argument("--edges-end", default="2020-12-31", help="分箱边缘的训练区间右端（含）")
    ap.add_argument("--horizon", type=int, default=10, help="前向窗口（应与样本一致）")
    ap.add_argument("--stop", type=float, default=0.05)
    ap.add_argument("--take", type=float, default=0.10)
    ap.add_argument("--include-neutral", action="store_true", help="把中性并入分母（默认排除）")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    setup_logging(args.verbose)
    samples_path = Path(args.samples)
    if not samples_path.is_file():
        logger.error("样本表不存在：%s（先跑 build_conviction_samples.py）", samples_path)
        raise SystemExit(2)
    # 只读聚合真正用得到的列：全量 ~10.7M 行，多读一列就把可用内存吃掉一截。
    # board 转 category：主板/创业板/科创板/北交所/场内基金就五类，object 存 10.7M 份浪费，groupby 也顺带更快。
    samples = pd.read_parquet(
        samples_path, columns=["date", "board", "label", *bucket.BIN_FEATURES])
    logger.info("读入样本 %d 行，列 %s", len(samples), list(samples.columns))
    if samples.empty:
        logger.error("样本表为空")
        raise SystemExit(2)
    if samples["board"].dtype == object:
        samples["board"] = samples["board"].astype("category")

    edges = compute_edges(samples, args.edges_end)
    binned = add_bins(samples, edges)
    table = summarize(binned, edges, args.horizon, args.stop, args.take, args.include_neutral)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    table.to_parquet(out, index=False)
    logger.info("已写 %s：%d 个格子（含中性=%s）", out, len(table), args.include_neutral)

    if not table.empty:
        top = table.sort_values("n", ascending=False).head(10)
        logger.info("样本量最大的 10 个格子：")
        for _, r in top.iterrows():
            logger.info("  %-4s b%d p%d v%d  n=%7d  止盈率=%.1f%% [%.1f,%.1f] 成功%d/失败%d/中性%d %s",
                        r["board"], r["bias_bin"], r["pos_bin"], r["vol_bin"], r["n"],
                        r["hit_rate"] * 100, r["ci_low"] * 100, r["ci_high"] * 100,
                        r["n_success"], r["n_failure"], r["n_neutral"],
                        "可信" if r["credible"] else "样本不足")


if __name__ == "__main__":
    main()
