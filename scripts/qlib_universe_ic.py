"""批次 N 的 G-N3：同一个模型给全市场逐日打分，比「训练池内 / 池外」两组的 5 日横截面 IC。

预注册在 stock_selector/docs/qlib路线数据口径与验收门槛.md §17.3，这里只执行、不重新定口径。
一句话动机：模型只在流动性前 300 只上训练过，归一化的 mean/std 也是那 300 只拟合的，
把分数用到池外票上属于**外推**。外推不是错，但没量过就不能当准。

所以把每一天按「当日这只票在不在旧训练池」劈成 A/B 两组，各算一次横截面 IC：
- A 组 = 当日在旧池 300 内（模型见过的票）
- B 组 = 当日不在旧池（放开池新增的那批）
采信线：**B 组均值 IC ≥ A 组均值 IC 的 70%**，且 B 组日数 ≥ 30。

B 组天然比 A 组「更薄、更小、更新」，所以只看 A vs B 会把「流动性差」误读成「外推不行」。
另外按当日 20 日均成交额把当天进榜的票分三档（T1 最流动 / T3 最不流动），
比 A-T1 vs B-T1 这一格，才知道差距是流动性带来的还是票池带来的。

用法：
    python3 scripts/qlib_universe_ic.py --smoke          # 冒烟：只跑 3 个名字块
    python3 scripts/qlib_universe_ic.py                  # 全量分块跑（可中断续跑，块文件落盘）
    python3 scripts/qlib_universe_ic.py --aggregate      # 读块文件出 IC 与裁决
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from stock_selector_qlib import artifacts, serving  # noqa: E402

LABEL_EXPR = None  # 运行时从 qlib_run 取，保证与训练口径逐字一致
AMT_EXPR = "Mean($amount, 20)"
MIN_NAMES_PER_DAY = 30  # 一天里某组少于 30 只，IC 就是噪声，不计入


def free_gb() -> float:
    info = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, rest = line.partition(":")
        info[key] = int(rest.split()[0])
    return info["MemAvailable"] / 1024 / 1024


def usable_days(days: list[str], beg: str, horizon: int) -> list[str]:
    """标签是 Ref($close,-5)/Ref($close,-1)-1，末日往前要留 horizon+1 个交易日，否则标签算不出来。"""
    lo = next(i for i, d in enumerate(days) if d >= beg)
    hi = len(days) - 1 - horizon
    return days[lo:hi + 1]


def score_block(art, syms: list[str], beg: str, end: str) -> pd.DataFrame:
    """给 syms 在 [beg,end] 逐日打分，并带上当日 20 日均成交额与「是否在旧池」。"""
    frame = serving.raw_features(syms, beg, end)
    keep = frame.isna().sum(axis=1) <= serving.MAX_NAN_FACTORS
    kept = frame[keep]
    if kept.empty:
        return pd.DataFrame()
    score = art.booster.predict(serving.normalize(kept, art))

    import qlib.data

    lab = qlib.data.D.features(list(syms), [LABEL_EXPR, AMT_EXPR], start_time=beg, end_time=end)
    # D.features 的索引是 (instrument, datetime)，Alpha158 handler 是 (datetime, instrument)，
    # 不对齐层级直接 reindex 会全部落空（静默 NaN，不报错）
    lab = lab.reorder_levels(["datetime", "instrument"]).sort_index().reindex(kept.index)
    out = pd.DataFrame({
        "score": score.astype("float32"),
        "label": lab.iloc[:, 0].to_numpy(dtype="float32"),
        "amt20": lab.iloc[:, 1].to_numpy(dtype="float32"),
    }, index=kept.index)
    rate = float(out["label"].notna().mean())
    if rate < 0.5:
        raise RuntimeError(f"标签只对上 {rate:.1%}，多半是索引层级或 label 表达式错了，不是行情缺")
    out.index = out.index.rename(["datetime", "instrument"])
    return out.reset_index()


def ic_day(sub: pd.DataFrame) -> float:
    """Spearman = 秩的 Pearson；先剔掉没有标签的行（停牌/退市/日线断档）。"""
    d = sub.dropna(subset=["label", "score"])
    if len(d) < MIN_NAMES_PER_DAY:
        return np.nan
    r1 = d["score"].rank().to_numpy()
    r2 = d["label"].rank().to_numpy()
    if np.allclose(r1.std(), 0) or np.allclose(r2.std(), 0):
        return np.nan
    return float(np.corrcoef(r1, r2)[0, 1])


def run(args) -> int:
    global LABEL_EXPR
    sys.path.insert(0, str(REPO / "scripts"))
    from qlib_run import LABEL

    LABEL_EXPR = LABEL
    art = artifacts.load()
    serving.init_qlib()
    if args.source == "universe":
        if not serving.UNIVERSE_FILE.exists():
            raise RuntimeError(f"缺少全市场名单 {serving.UNIVERSE_FILE}，先跑 scripts/qlib_daily_universe.py")
        universe = [x.strip() for x in serving.UNIVERSE_FILE.read_text(encoding="utf-8").splitlines()
                    if x.strip()]
    else:
        universe = list(art.pool)
    if args.limit_universe:
        universe = universe[:args.limit_universe]
    pool_set = set(art.pool)
    days = usable_days(serving.calendar(), args.beg, args.horizon)
    print(f"名单 {len(universe)} 只（source={args.source}），旧池 {len(pool_set)} 只，"
          f"重放 {days[0]}→{days[-1]} 共 {len(days)} 个交易日", flush=True)
    args.out.mkdir(parents=True, exist_ok=True)

    date_blocks = [days[i:i + args.days_chunk] for i in range(0, len(days), args.days_chunk)]
    done = skipped = 0
    for bi, block in enumerate(date_blocks, 1):
        dest = args.out / f"dates_{block[0]}_{block[-1]}.parquet"
        if dest.exists() and not args.force:
            skipped += 1
            print(f"[{bi}/{len(date_blocks)}] {block[0]}→{block[-1]} 已存在，跳过", flush=True)
            continue
        avail = free_gb()
        if avail < args.min_free_gb:
            print(f"内存水位 {avail:.1f} GB < {args.min_free_gb}，停在第 {bi} 个日期块，"
                  f"已完成的块可以直接 --aggregate", flush=True)
            return 2
        t0 = time.time()
        parts = []
        for start in range(0, len(universe), args.names_chunk):
            batch = universe[start:start + args.names_chunk]
            df = score_block(art, batch, block[0], block[-1])
            if df.empty:
                continue
            df["in_pool"] = df["instrument"].isin(pool_set)
            parts.append(df)
            if args.smoke and len(parts) >= args.smoke:
                break
        if not parts:
            raise RuntimeError(f"{block[0]}→{block[-1]} 一个名字都没打上分")
        tidy = pd.concat(parts, ignore_index=True)
        tidy["datetime"] = tidy["datetime"].astype(str)
        tidy.to_parquet(dest, index=False)
        done += 1
        print(f"[{bi}/{len(date_blocks)}] {block[0]}→{block[-1]} 行数 {len(tidy):,} "
              f"进榜 {tidy.groupby('datetime').size().median():.0f} 只/日 "
              f"耗时 {time.time() - t0:.0f} 秒 内存峰值余量 {free_gb():.1f} GB → {dest.name}", flush=True)
        if args.smoke:
            break
    print(f"完成 {done} 个日期块，跳过 {skipped} 个", flush=True)
    return 0


def aggregate(args) -> int:
    files = sorted(args.out.glob("dates_*.parquet"))
    if not files:
        print("还没有任何日期块，先不带 --aggregate 跑一轮")
        return 1
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    if args.daterange_from:
        df = df[df["datetime"] >= args.daterange_from]
    if args.daterange_to:
        df = df[df["datetime"] <= args.daterange_to]
    print(f"读入 {len(files)} 块 / 取 {args.daterange_from or '最早'}→{args.daterange_to or '最晚'}"
          f" 共 {df['datetime'].nunique()} 个交易日 / {len(df):,} 行", flush=True)
    if args.exclude_codes:
        # 现名 ST/退 的票必须能整批摘出去重算：它们涨跌停是 ±5%、还带退市风险，
        # 留在 B 组里会把「池外不行」和「ST 不行」混成一件事（见文档 §17.7）
        drop = {line.split("\t")[0].strip()[-6:] for line in
                Path(args.exclude_codes).read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.startswith("#")}
        before = len(df)
        df = df[~df["instrument"].astype(str).str[-6:].isin(drop)]
        print(f"按 {args.exclude_codes}（{len(drop)} 只现名 ST/退）再剔 {before - len(df):,} 行"
              f" → 剩 {len(df):,} 行", flush=True)
    if df.empty:
        return 1
    df = df.dropna(subset=["label", "score"])
    # 当日流动性分档：只在「有 amount 的票」之间排，缺失的单独一档，别混进 T3
    df["amt_pct"] = df.groupby("datetime")["amt20"].rank(pct=True)
    df["tier"] = np.select(
        [df["amt_pct"].isna(), df["amt_pct"] >= 2 / 3, df["amt_pct"] >= 1 / 3],
        ["NA", "T1", "T2"], default="T3")

    rows = []
    for date, day in df.groupby("datetime"):
        rec = {"date": date}
        for tag, sub in (("all", day), ("A", day[day.in_pool]), ("B", day[~day.in_pool])):
            rec[f"n_{tag}"] = int(len(sub))
            rec[f"ic_{tag}"] = ic_day(sub)
        for tier in ("T1", "T2", "T3", "NA"):
            for grp, sub in ((f"A_{tier}", day[(day.in_pool) & (day.tier == tier)]),
                             (f"B_{tier}", day[(~day.in_pool) & (day.tier == tier)])):
                rec[f"n_{grp}"] = int(len(sub))
                rec[f"ic_{grp}"] = ic_day(sub)
        rows.append(rec)
    daily = pd.DataFrame(rows).sort_values("date")
    if daily.empty:
        print("这个窗口里一天都聚不出 IC（组内名字数不足 30）")
        return 1
    daily.to_csv(args.daily_out, index=False)
    paired = daily.dropna(subset=["ic_A", "ic_B"])

    mean_a, mean_b = daily["ic_A"].mean(), daily["ic_B"].mean()
    d = (paired["ic_B"] - paired["ic_A"]).to_numpy()
    t_raw = d.mean() / (d.std(ddof=1) / np.sqrt(len(d))) if len(d) > 2 else float("nan")
    # 5 日重叠标签 ⇒ 相邻两天的 IC 用的是几乎同一批收益，配对 t 要 ÷√5（§11 的口径）
    t_adj = t_raw / np.sqrt(5)
    print(f"\n=== G-N3 读数 {daily['date'].min()}→{daily['date'].max()}："
          f"{len(daily)} 个交易日，A/B 同时有数的 {len(paired)} 天 ===")
    print(f"A 组（当日在旧池）均值 IC {mean_a:+.4f}  日均 {daily['n_A'].mean():.0f} 只")
    print(f"B 组（当日池外）  均值 IC {mean_b:+.4f}  日均 {daily['n_B'].mean():.0f} 只")
    print(f"全名单          均值 IC {daily['ic_all'].mean():+.4f}")
    print(f"配对差 B−A 均值 {d.mean():+.5f}，t={t_raw:.2f}，重叠修正后 t/√5={t_adj:.2f}")
    print("\n按当日 20 日均成交额分档（T1 最流动）：")
    print(f"{'档':<4}{'A 均值 IC':>12}{'A 天数':>8}{'B 均值 IC':>12}{'B 天数':>8}{'B/A':>8}")
    for tier in ("T1", "T2", "T3", "NA"):
        a, b = daily[f"ic_A_{tier}"], daily[f"ic_B_{tier}"]
        ratio = b.mean() / a.mean() if len(a.dropna()) and a.mean() else float("nan")
        print(f"{tier:<4}{a.mean():>+12.4f}{int(a.notna().sum()):>8}"
              f"{b.mean():>+12.4f}{int(b.notna().sum()):>8}{ratio:>8.3f}")
    if np.isnan(mean_a) or np.isnan(mean_b):
        verdict = "无法判定（某一组凑不出够多名字的天数）"
    else:
        print(f"比值 B/A = {mean_b / mean_a:.3f}   采信线 0.700")
        verdict = ("达标" if (mean_a > 0 and mean_b >= 0.7 * mean_a and len(paired) >= 30)
                   else "不达线")
        if mean_a <= 0:
            verdict += "（A 组均值 IC 不为正，比值没有意义，按不达线处理）"
    print(f"\n采信线判定：B 组均值 IC ≥ A 组的 70% 且 B 组日数 ≥ 30 ⇒ {verdict}")
    print(f"逐日明细 → {args.daily_out}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--beg", default="2023-01-01")
    p.add_argument("--horizon", type=int, default=6, help="标签要往后看的交易日数（5 日标签 + Ref(-1)）")
    p.add_argument("--source", choices=["universe", "model"], default="universe",
                   help="重放用的名单；G-N3 要的是 universe（全市场名单）")
    p.add_argument("--names-chunk", type=int, default=700)
    p.add_argument("--days-chunk", type=int, default=120, help="一个日期块多少个交易日，决定块文件粒度")
    p.add_argument("--limit-universe", type=int, default=0)
    p.add_argument("--smoke", type=int, default=0, help="冒烟：每个日期块只跑这么多个名字块就停")
    p.add_argument("--min-free-gb", type=float, default=3.0)
    p.add_argument("--force", action="store_true")
    p.add_argument("--aggregate", action="store_true")
    p.add_argument("--from", dest="daterange_from", default="",
                   help="只统计这个日期之后的 IC（G-N3 要用 train_end 之后的样本外窗口）")
    p.add_argument("--to", dest="daterange_to", default="")
    p.add_argument("--exclude-codes", default="",
                   help="一个代码一行的清单，聚数前整批剔掉（G-N3 用它把现名 ST/退 的票摘出去单独再判一次）")
    p.add_argument("--out", type=Path, default=REPO / "data" / "qlib" / "gn3")
    p.add_argument("--daily-out", type=Path, default=REPO / "data" / "qlib" / "gn3_daily_ic.csv")
    args = p.parse_args()
    return aggregate(args) if args.aggregate else run(args)


if __name__ == "__main__":
    sys.exit(main())
