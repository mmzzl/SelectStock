"""卖出规则审计（只读、只打印，不落盘）：给「持有可信度」找一个有依据的定义。

为什么要这张表（2026-10-16 的口径纠正）：
方案 B 原本设想「持有可信度 = 全部未触发的卖出规则，它们历史卖飞/误卖统计的汇总」。这个汇总
**在数学上不成立**：一条规则的「触发后表现」和「未触发时的表现」是两个不同的条件分布，
把未触发规则的触发后统计相加，得到的不是「什么都没触发时持有的结果」。未触发态必须**直接量**。
而更要紧的是：现行判据只有**一条**趋势卖出规则（`price <= ma10*0.95`，见 portfolio/rules.py，
未标定、抄自测试夹具）+ 一条要成本价的灾难线。所以「几条规则一起没响」这个状态根本不存在，
未触发态 = hold ∪ add，它只能整体量一次。

本脚本量的就是这个整体状态，以及它内部能不能再分层。统计全部用**同日横截面超额**
（同期全体可判点均值已扣掉），否则符号由大盘决定（2006–2026 全样本 10 日均值只有 +0.08%）。

口径：
- 触发：与插件 `judge_holding` 同一条串，`close <= ma10 * 0.95`；
- 收益：T+10 / T+20 交易日收盘收益（不含成本）；
- 灾难：未来 20 根内（不含当天）最低价相对 today close 的回撤 ≤ −8%（对齐灾难线口径），
  另给 ≤ −12% 的深尾；
- 兑现率：触发后 T+20 收盘 < 0 的比例；卖飞率：触发后 T+20 收盘 > +3% 的比例；
- 时代：日期三分位（早/中/近）；抽样：代码序号对半切；两轴都要同号才敢当结论。

局限（不是 bug，是量不到的东西）：
- 灾难线 −8% 要成本价，全市场没有「假想成本价」，所以这里量的是**价格回撤**，不是浮亏；
- 台账里 hold 有 46% 在 20 日内自己变成 sell（`flip_sell_20`），「没有警报」并不是一个稳定状态，
  这一点比任何单个概率都更接近「持有可信度」的真相。

用法:
    python scripts/audit_sell_rules.py                 # 全量（~6,600 个日线文件，几分钟）
    python scripts/audit_sell_rules.py --step 3        # 每 3 个文件取 1 个，快速看形状
    python scripts/audit_sell_rules.py --from 2019-01-01
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_selector.conviction import bucket  # noqa: E402

KLINE_DIR = ROOT / "data" / "stock_kline"
LOG_DIR = ROOT / "logs"

COLUMNS = ["name", "date", "close", "ma10", "ma5", "rsi14", "pos60", "atr14", "low"]
FWD_SHORT = 10
FWD_LONG = 20
SELL_RULE = "close <= ma10 * 0.95"      # 与 portfolio/rules.py DEFAULT_SELL_RULE 同形（price≡close）
FLIP_GAIN = 0.03                         # 「卖飞」= 触发后 20 日反涨超过这个
TAIL_LINES = (-0.08, -0.12)              # 灾难口径：未来 20 日内最深回撤阈值
MIN_CELL = 3000

logger = logging.getLogger("audit_sell_rules")


def setup_logging(verbose: bool) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in list(logger.handlers):
        logger.removeHandler(h)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)


def rev_min(arr: np.ndarray, win: int) -> np.ndarray:
    """min(arr[i .. i+win-1])（含当天）；不足窗口的尾部留 NaN。"""
    out = np.full(arr.shape, np.nan, dtype=np.float32)
    if len(arr) >= win:
        r = pd.Series(arr[::-1]).rolling(win, min_periods=win).min().to_numpy()[::-1]
        out[: len(r)] = r.astype(np.float32)
    return out


def collect(files: list[Path]) -> pd.DataFrame:
    """逐票流式读取 → 长表。每票只留可判行，避免全市场稠密矩阵（机器上还有线上服务）。

    全量 ~12M 行，日期存 int32 天序、代码存 int16 下标、字符串列一律 category：
    按 str 存 12M 个 code/board 会白吃 1GB 以上，而这台机器当时只剩 2GB 可用。
    """
    frames = []
    t0 = time.time()
    for i, path in enumerate(files):
        try:
            df = pd.read_parquet(path, columns=COLUMNS)
        except Exception as exc:
            logger.debug("跳过 %s：%s", path.name, exc)
            continue
        if len(df) < 60:
            continue
        name = str(df["name"].iloc[-1] or "")
        if bucket.is_st_name(name):
            continue                                    # ST 未建模，灾难线口径也不同
        close = df["close"].to_numpy(dtype=np.float32)
        ma10 = df["ma10"].to_numpy(dtype=np.float32)
        low = df["low"].to_numpy(dtype=np.float32)
        judged = np.isfinite(close) & (close > 0) & np.isfinite(ma10)

        fwd10 = np.full(close.shape, np.nan, dtype=np.float32)
        fwd10[: len(close) - FWD_SHORT] = close[FWD_SHORT:] / close[: len(close) - FWD_SHORT] - 1.0
        fwd20 = np.full(close.shape, np.nan, dtype=np.float32)
        fwd20[: len(close) - FWD_LONG] = close[FWD_LONG:] / close[: len(close) - FWD_LONG] - 1.0
        m20 = rev_min(low, FWD_LONG)
        dd20 = np.full(close.shape, np.nan, dtype=np.float32)
        dd20[: len(close) - 1] = m20[1:] / close[: len(close) - 1] - 1.0   # 不含当天

        sell = judged & (close <= ma10 * np.float32(0.95))
        add = judged & ~sell & (close >= df["ma5"].to_numpy(np.float32) * np.float32(1.03)) \
            & (df["rsi14"].to_numpy(np.float32) <= 65)                     # sell 优先，与 judge 一致
        atr = df["atr14"].to_numpy(dtype=np.float32)
        keep = judged & np.isfinite(fwd20) & np.isfinite(dd20)
        day = pd.to_datetime(df["date"]).to_numpy(dtype="datetime64[D]").astype("int32")
        frames.append(pd.DataFrame({
            "day": day[keep],
            "code_id": np.int16(i % 2),                   # 抽样轴：代码序号对半
            "board": bucket.classify_board(path.stem),
            "state": np.where(sell, "sell", np.where(add, "add", "hold"))[keep],
            "dist": (close / (ma10 * np.float32(0.95)) - 1.0).astype(np.float32)[keep],
            "vol_pct": np.where(atr > 0, atr / close, np.nan).astype(np.float32)[keep],
            "pos60": df["pos60"].to_numpy(dtype=np.float32)[keep],
            "fwd10": fwd10[keep], "fwd20": fwd20[keep], "dd20": dd20[keep],
        }))
        if (i + 1) % 1000 == 0 or i + 1 == len(files):
            logger.info("读 %d/%d 票，累计 %d 行 (%.0fs)", i + 1, len(files),
                        sum(len(f) for f in frames), time.time() - t0)
    if not frames:
        raise SystemExit("没有可判行")
    out = pd.concat(frames, ignore_index=True)
    del frames
    out["board"] = out["board"].astype("category")
    out["state"] = out["state"].astype("category")
    # 同日全体可判点均值 = 横截面基准；扣掉它才敢比时代/抽样。
    for src, dst in (("fwd10", "exc10"), ("fwd20", "exc20")):
        out[dst] = (out[src] - out.groupby("day")[src].transform("mean")).astype(np.float32)
    day_ord = out["day"].to_numpy(dtype=np.int64)
    cuts = np.quantile(day_ord, [1 / 3, 2 / 3])
    out["era"] = np.select([day_ord < cuts[0], day_ord >= cuts[1]], [0, 2], default=1)
    logger.info("可判 %d 行 / %d 票 / %s→%s", len(out), len(files),
                pd.Timestamp(int(day_ord.min()), unit="D").date(),
                pd.Timestamp(int(day_ord.max()), unit="D").date())
    return out


def cell(sub: pd.DataFrame) -> dict:
    n = len(sub)
    f20, dd = sub["fwd20"].to_numpy(), sub["dd20"].to_numpy()
    return {
        "n": n,
        "fwd10": float(sub["fwd10"].mean()) * 100,
        "exc10": float(sub["exc10"].mean()) * 100,
        "exc20": float(sub["exc20"].mean()) * 100,
        "down20": float((f20 < 0).mean()) * 100,
        "flip20": float((f20 > FLIP_GAIN).mean()) * 100,
        "tail8": float((dd <= TAIL_LINES[0]).mean()) * 100,
        "tail12": float((dd <= TAIL_LINES[1]).mean()) * 100,
    }


def fmt(name: str, r: dict, base: dict) -> str:
    return (f"  {name:22s} n={r['n']:9,}  T+10 {r['fwd10']:+6.2f}%  超额10 {r['exc10']:+6.2f}pp  "
            f"超额20 {r['exc20']:+6.2f}pp  20日跌 {r['down20']:5.1f}%  卖飞 {r['flip20']:5.1f}%  "
            f"≤−8% {r['tail8']:5.2f}% (lift {r['tail8'] / base['tail8']:4.2f})  "
            f"≤−12% {r['tail12']:5.2f}% (lift {r['tail12'] / base['tail12']:4.2f})")


def stratify(df: pd.DataFrame, base: dict, col: str, edges: list[float], labels: list[str]) -> None:
    logger.info("hold 态按 %s 分层（看灾难率 lift 是否有单调性）：", col)
    hold = df[df["state"] == "hold"]
    prev = None
    for lo, hi, lab in zip(edges[:-1], edges[1:], labels):
        sub = hold[(hold[col] >= lo) & (hold[col] < hi)]
        if len(sub) < MIN_CELL:
            logger.info("  %-12s n=%9d —— 样本不足", lab, len(sub))
            continue
        r = cell(sub)
        lift8 = r["tail8"] / base["tail8"]
        mono = "" if prev is None else ("↑" if lift8 > prev else "↓")
        prev = lift8
        logger.info(fmt(lab + mono, r, base))


def report(df: pd.DataFrame) -> None:
    base = cell(df)
    logger.info("════ 卖出规则审计（口径见文件头；T+10/T+20 收盘收益，不含成本）════")
    logger.info("全体可判基准：" + fmt("ALL", base, base))
    logger.info("（exc10/exc20 = 扣掉同日全体均值后的超额，单位 pp；理论全体≈0）")

    logger.info("—— 按判定状态（sell=唯一那条规则触发；hold=什么都没响）——")
    for st in ("sell", "add", "hold"):
        sub = df[df["state"] == st]
        if len(sub) >= MIN_CELL:
            logger.info(fmt(st, cell(sub), base))

    logger.info("—— sell 态分时代 / 分抽样（同号才敢用）——")
    for st in ("sell", "hold"):
        for col, names in (("era", ("早", "中", "近")), ("code_id", ("抽样A", "抽样B"))):
            parts = []
            for i, nm in enumerate(names):
                sub = df[(df["state"] == st) & (df[col] == i)]
                if len(sub) >= MIN_CELL:
                    r = cell(sub)
                    parts.append(f"{nm}:超额{r['exc20']:+.2f}pp/≤−8%{r['tail8']:.2f}%")
            logger.info("  %-5s %-8s %s", st, col, "  ".join(parts))

    logger.info("—— sell 态按板块（基金波动几何不同，别和股票混）——")
    for b, grp in df[df["state"] == "sell"].groupby("board", observed=True):
        if len(grp) >= MIN_CELL:
            logger.info(fmt(f"sell/{b}", cell(grp), base))

    qs = list(np.quantile(df.loc[df["state"] == "hold", "dist"], np.linspace(0, 1, 11)))
    qs[0], qs[-1] = -1e9, 1e9
    stratify(df, base, "dist", qs, [f"距卖点线D{i}" for i in range(10)])
    stratify(df, base, "vol_pct", [0, .01, .015, .02, .03, 1e9],
             ["ATR%<1", "1-1.5", "1.5-2", "2-3", "≥3"])
    stratify(df, base, "pos60", [0, .2, .4, .6, .8, 1.01],
             ["pos60<0.2", "0.2-0.4", "0.4-0.6", "0.6-0.8", "≥0.8"])

    hold, sell = df[df["state"] == "hold"], df[df["state"] == "sell"]
    logger.info("════ 结论读数 ════")
    logger.info("  hold 相对全体：超额20 %+0.2fpp，灾难率(≤−8%%) %.2f%% vs 基准 %.2f%%（lift %.2f）",
                cell(hold)["exc20"], cell(hold)["tail8"], base["tail8"],
                cell(hold)["tail8"] / base["tail8"])
    logger.info("  sell 相对 hold：超额20 %+0.2fpp，灾难率 %.2f%% vs %.2f%%（lift %.2f）",
                cell(sell)["exc20"] - cell(hold)["exc20"], cell(sell)["tail8"],
                cell(hold)["tail8"], cell(sell)["tail8"] / max(cell(hold)["tail8"], 1e-9))


def main() -> None:
    ap = argparse.ArgumentParser(description="卖出规则 / 持有可信度审计（只读）")
    ap.add_argument("--kline-dir", default=str(KLINE_DIR))
    ap.add_argument("--step", type=int, default=1, help="每 N 个文件取 1 个（抽样看形状）")
    ap.add_argument("--from", dest="date_from", default="", help="只统计这天及以后的行")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    setup_logging(args.verbose)

    files = sorted(Path(args.kline_dir).glob("*.parquet"))[:: max(1, args.step)]
    logger.info("扫描 %d 个日线文件（step=%d）", len(files), args.step)
    df = collect(files)
    if args.date_from:
        cut = (pd.Timestamp(args.date_from) - pd.Timestamp("1970-01-01")).days
        df = df[df["day"] >= cut]
        logger.info("按 --from %s 过滤后 %d 行", args.date_from, len(df))
    report(df)


if __name__ == "__main__":
    main()
