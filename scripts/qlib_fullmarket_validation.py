"""全市场动态票池 · topk=1 / topk=10 的样本外验证。

预注册与裁决线写在 `stock_selector/docs/qlib全市场topk验证_20261025.md`；本脚本只执行，
不在读数之后改窗口、改门槛、加臂。

和 `qlib_guardrail_ab.py` / `qlib_hold_rule_ab.py` 的区别：
- 票池不是今天的 `universe.txt` 固定回放，而是**逐日**按「上市满 130 根 + 过去 20 日均额 ≥1 亿」
  重算；今天才流动的票不会穿越到 2024 年，退市/停牌票也会在当天从名单消失。
- 同一份分数面板复用给 topk=1 和 topk=10，打分只算一次。
- 成本有基准和滑点压力两档；所有收益都是扣成本净值（qlib `account` 列）。

用法：
    python3 -u scripts/qlib_fullmarket_validation.py --prepare-only
    python3 -u scripts/qlib_fullmarket_validation.py
    python3 -u scripts/qlib_fullmarket_validation.py --reuse-scores
    # 止盈止损研究臂：复用已落盘分数，只换出场规则
    python3 -u scripts/qlib_fullmarket_validation.py --reuse-scores \
        --scores-path data/qlib/fullmarket_validation_20261025_scores.parquet \
        --out data/qlib/fullmarket_validation_20261025_sl8tp10 \
        --stop-loss 0.08 --take-profit 0.10

冒烟（只验管线，不是读数）：
    python3 -u scripts/qlib_fullmarket_validation.py --smoke --limit-symbols 60 \
        --start 2026-07-01 --end 2026-08-31
"""
from __future__ import annotations

import argparse
import gc
import json
import math
import os
import resource
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from qlib_daily_universe import panel_calendar, panel_stocks  # noqa: E402
from stock_selector_qlib import artifacts, guardrails, serving  # noqa: E402
import stock_selector_qlib.backtest as bt  # noqa: E402
from stock_selector_qlib.backtest import _snap_window, run_qlib_backtest  # noqa: E402

OUT_DEFAULT = REPO / "data" / "qlib" / "fullmarket_validation_20261025"
ST_FILE = REPO / "data" / "qlib" / "st_codes_20261018.txt"
TRADING_DAYS = 243


def free_gb() -> float:
    info = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, rest = line.partition(":")
        info[key] = int(rest.split()[0])
    return info["MemAvailable"] / 1024 / 1024


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def load_st_codes(path: Path) -> set[str]:
    out: set[str] = set()
    if not path.exists():
        raise FileNotFoundError(f"缺 ST 清单：{path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.add(line.split("\t")[0].strip().upper())
    if not out:
        raise RuntimeError(f"ST 清单是空的：{path}")
    return out


def _read_bin(path: Path, n: int) -> np.ndarray:
    """qlib bin：首格是起始日历下标，后面是值；越界部分丢掉，缺格保持 NaN。"""
    out = np.full(n, np.nan, dtype=np.float32)
    if not path.exists():
        return out
    arr = np.fromfile(path, dtype="<f")
    if arr.size < 2:
        return out
    start = int(arr[0])
    vals = arr[1:]
    lo, hi = max(0, start), min(n, start + len(vals))
    if hi > lo:
        out[lo:hi] = vals[lo - start:hi - start]
    return out


def load_panel(symbols: list[str], cal: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """一次读完整 bin 面板（约 5,200 只 × 2,867 天），float32 约 120 MB。"""
    n, m = len(cal), len(symbols)
    close = np.full((n, m), np.nan, dtype=np.float32)
    amount = np.full((n, m), np.nan, dtype=np.float32)
    feats = Path("/data/work/qlib_data/cn/features")
    t0 = time.time()
    for j, sym in enumerate(symbols):
        base = feats / sym.lower()
        close[:, j] = _read_bin(base / "close.day.bin", n)
        amount[:, j] = _read_bin(base / "amount.day.bin", n)
        if (j + 1) % 1000 == 0:
            print(f"  读 bin {j + 1}/{m}，{time.time() - t0:.1f}s，RSS {rss_mb():.0f} MB", flush=True)
    close[close <= 0] = np.nan
    amount[amount <= 0] = np.nan
    idx = pd.to_datetime(cal)
    return (pd.DataFrame(close, index=idx, columns=symbols),
            pd.DataFrame(amount, index=idx, columns=symbols))


def build_universes(close: pd.DataFrame, amount: pd.DataFrame, start: str, end: str,
                    st_codes: set[str], min_bars: int, min_amount: float
                    ) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """逐日可交易域；返回 (不剔 ST, 剔 ST, 统计)。

    `bars` 和 `amt20` 都只看当日及以前；`close.notna()` 保证当天有行情。
    """
    bars = close.notna().cumsum()
    amt20 = amount.rolling(20, min_periods=10).mean()
    base = (bars >= int(min_bars)) & close.notna() & (amt20 >= float(min_amount))
    all_u = base.loc[start:end].copy()
    st_cols = [c for c in all_u.columns if c in st_codes]
    st_u = all_u.copy()
    if st_cols:
        st_u.loc[:, st_cols] = False
    stats = {
        "min_bars": int(min_bars),
        "min_amount": float(min_amount),
        "st_codes": len(st_codes),
        "st_columns_in_panel": len(st_cols),
        "days": int(len(all_u)),
        "union_all": int(all_u.any().sum()),
        "union_st": int(st_u.any().sum()),
        "daily_all": _daily_stats(all_u),
        "daily_st": _daily_stats(st_u),
        "first_day_st": int(st_u.iloc[0].sum()),
        "last_day_st": int(st_u.iloc[-1].sum()),
    }
    return all_u, st_u, stats


def _daily_stats(elig: pd.DataFrame) -> dict:
    s = elig.sum(axis=1)
    return {"min": int(s.min()), "median": int(s.median()), "max": int(s.max()),
            "mean": round(float(s.mean()), 1)}


def score_union(art, union: list[str], warm_beg: str, end: str, chunk: int,
                dest: Path, reuse: bool) -> tuple[pd.Series, dict]:
    """按名字块打分，每块落盘；中断后 --reuse-scores 只补缺块。

    不用 `serving.score_frame` 的整段循环，是因为全市场 2.8 年要跑近一小时，
    没有进度和断点续跑会把一次内存抖动变成整段重跑。
    """
    if reuse and dest.exists():
        print(f"复用分数面板：{dest}", flush=True)
        pred = pd.read_parquet(dest)["score"]
        return pred, {"reused": True, "scored": int(len(pred))}

    chunk_dir = dest.with_name(dest.stem + "_chunks")
    chunk_dir.mkdir(parents=True, exist_ok=True)
    parts: list[pd.Series] = []
    thin = missing = 0
    t0 = time.time()
    n_chunks = (len(union) + chunk - 1) // chunk
    for i, start in enumerate(range(0, len(union), chunk), 1):
        batch = union[start:start + chunk]
        part_path = chunk_dir / f"{start:05d}_{batch[0]}_{batch[-1]}.parquet"
        if reuse and part_path.exists():
            part = pd.read_parquet(part_path)["score"]
        else:
            if free_gb() < 2.0:
                raise RuntimeError(f"可用内存 {free_gb():.1f} GB < 2 GB，停在块 {i}/{n_chunks}；"
                                   "已落盘的块可用 --reuse-scores 续跑")
            frame = serving.raw_features(batch, warm_beg, end)
            keep = frame.isna().sum(axis=1) <= serving.MAX_NAN_FACTORS
            thin += int((~keep).sum())
            if not keep.any():
                missing += len(batch)
                part = None
            else:
                kept = frame[keep]
                part = pd.Series(art.booster.predict(serving.normalize(kept, art)),
                                 index=kept.index)
                part.rename("score").to_frame().to_parquet(part_path)
            del frame
            gc.collect()
        if part is not None and len(part):
            parts.append(part)
        elapsed = time.time() - t0
        eta = elapsed / i * (n_chunks - i)
        print(f"  打分块 {i}/{n_chunks}（{batch[0]}…{batch[-1]}）："
              f"{0 if part is None else len(part):,} 行，{elapsed:.0f}s，ETA {eta:.0f}s，"
              f"RSS {rss_mb():.0f} MB，可用 {free_gb():.1f} GB", flush=True)
    if not parts:
        raise RuntimeError("全市场打分一行都没有；先查因子面板/模型列")
    pred = pd.concat(parts).reorder_levels(["datetime", "instrument"]).sort_index()
    stats = {"pool": len(union), "chunk": chunk, "thin_rows": thin, "no_row": missing,
             "scored": int(len(pred)), "seconds": round(time.time() - t0, 1),
             "rss_mb": round(rss_mb(), 0), "chunks": n_chunks}
    dest.parent.mkdir(parents=True, exist_ok=True)
    pred.rename("score").to_frame().to_parquet(dest)
    print(f"打分 {len(union)} 只 × {warm_beg}→{end}：{stats['scored']:,} 行，"
          f"{stats['seconds']}s，RSS {stats['rss_mb']:.0f} MB → {dest}", flush=True)
    return pred, stats


def build_signal(pred: pd.Series, elig: pd.DataFrame, close: pd.DataFrame,
                 amount: pd.DataFrame, label: str) -> tuple[pd.Series, dict]:
    """把逐日票池、两条护栏套到分数上；返回稀疏 signal（只含当天能买的票）。"""
    amt20 = amount.rolling(20, min_periods=10).mean()
    yao = guardrails.spike_fade(close)
    parts: list[pd.Series] = []
    days = 0
    raw_rows = kept_rows = thin_rows = yao_rows = 0
    t0 = time.time()
    for day, g in pred.groupby(level="datetime"):
        day = pd.Timestamp(day)
        if day not in elig.index:
            continue
        g = g.droplevel("datetime")
        allowed = elig.columns[elig.loc[day].to_numpy(dtype=bool)]
        s = g.reindex(allowed).dropna()
        if s.empty:
            continue
        days += 1
        raw_rows += len(s)
        a = amt20.loc[day, s.index]
        q = a.quantile(0.25)
        thin = a.lt(q).fillna(False)
        y = yao.loc[day, s.index].fillna(False).astype(bool)
        keep = ~(thin | y)
        thin_rows += int(thin.sum())
        yao_rows += int(y.sum())
        kept = s[keep]
        kept_rows += len(kept)
        if not kept.empty:
            kept.index = pd.MultiIndex.from_product([[day], kept.index],
                                                     names=["datetime", "instrument"])
            parts.append(kept)
    if not parts:
        raise RuntimeError(f"{label}：一天都没有可买票")
    signal = pd.concat(parts).sort_index()
    stats = {"label": label, "days": days, "raw_rows": raw_rows, "kept_rows": kept_rows,
             "thin_dropped": thin_rows, "yao_dropped": yao_rows,
             "seconds": round(time.time() - t0, 1)}
    print(f"信号 {label}：{days} 天，{raw_rows:,} → {kept_rows:,} 行，"
          f"薄流动 {thin_rows:,}、妖痕 {yao_rows:,}，{stats['seconds']}s", flush=True)
    return signal, stats


def run_arm(signal: pd.Series, union: list[str], start: str, end: str, capital: float,
            benchmark: str, topk: int, entry_rank: int, exit_rank: int,
            costs: dict, label: str, stop_loss: float = 0.0, take_profit: float = 0.0):
    old_costs = bt.EXCHANGE_KWARGS
    old_pool = serving.score_pool
    old_source = serving.POOL_SOURCE
    bt.EXCHANGE_KWARGS = dict(costs)
    serving.score_pool = lambda _art: list(union)
    serving.POOL_SOURCE = "universe"
    params = {"start": start, "end": end, "capital": capital, "engine": "qlib",
              "benchmark": benchmark, "hold_rule": "band", "topk": int(topk),
              "entry_rank": int(entry_rank), "exit_rank": int(exit_rank),
              "stop_loss_pct": float(stop_loss), "take_profit_pct": float(take_profit)}
    try:
        t0 = time.time()
        result = run_qlib_backtest(params, signal=signal)
        result.executability.update({
            "pool_source": "dynamic_universe",
            "pool_size": len(union),
            "guardrails": True,
            "guardrails_applied": True,
            "live_parity": False,
            "parity_note": ("动态历史票池（逐日上市满 130 根 + 20 日均额 ≥1 亿 + 现名 ST 剔除）；"
                            "不是今天的 universe.txt 回放，不能当线上同口径背书"),
            "signal_lag": "T 日收盘成交、读 T−1 日分数（qlib 日历 shift=1）",
            "return_caliber": "扣成本净值（report['account']）",
            "stop_loss_pct": float(stop_loss),
            "take_profit_pct": float(take_profit),
            "stop_note": "日频按收盘判断/同一收盘成交，盘中破位、跳空、跌停卖不掉都不在模型里",
        })
        print(f"  撮合 {label}：{time.time() - t0:.0f}s，"
              f"净值 {result.metrics['total_return']:+.2%}，卖出 {result.metrics['sell_count']} 笔",
              flush=True)
        return result
    finally:
        bt.EXCHANGE_KWARGS = old_costs
        serving.score_pool = old_pool
        serving.POOL_SOURCE = old_source


def curve(result) -> pd.Series:
    return pd.Series({pd.Timestamp(e["date"]): float(e["total_value"])
                      for e in result.equity}).sort_index()


def benchmark_curve(benchmark: str, start: str, end: str) -> pd.Series:
    import qlib.data

    frame = qlib.data.D.features([benchmark], ["$close"], start_time=start, end_time=end)["$close"]
    if isinstance(frame.index, pd.MultiIndex):
        frame = frame.droplevel("instrument")
    frame.index = pd.to_datetime(frame.index)
    closes = frame.sort_index().dropna()
    if closes.empty:
        raise RuntimeError(f"基准 {benchmark} 在 {start}→{end} 没有行情")
    return closes


def subperiod(values: pd.Series, bench: pd.Series, start: str | None = None) -> dict:
    if start:
        values = values[values.index >= pd.Timestamp(start)]
        bench = bench[bench.index >= pd.Timestamp(start)]
    idx = values.index.intersection(bench.index)
    values, bench = values.loc[idx].dropna(), bench.loc[idx].dropna()
    idx = values.index.intersection(bench.index)
    values, bench = values.loc[idx], bench.loc[idx]
    if len(values) < 2:
        return {"days": int(len(values))}
    nav, bnav = values / values.iloc[0], bench / bench.iloc[0]
    n = len(nav)
    total = float(nav.iloc[-1] - 1.0)
    btotal = float(bnav.iloc[-1] - 1.0)
    annual = (1.0 + total) ** (TRADING_DAYS / n) - 1.0 if n else None
    bannual = (1.0 + btotal) ** (TRADING_DAYS / n) - 1.0 if n else None
    daily = nav.pct_change().dropna()
    bdaily = bnav.pct_change().dropna()
    excess = (daily - bdaily).dropna()
    t = (float(excess.mean()) / float(excess.std(ddof=1)) * math.sqrt(len(excess))
         if len(excess) > 2 and float(excess.std(ddof=1)) else None)
    return {
        "days": int(n),
        "start": str(values.index[0])[:10],
        "end": str(values.index[-1])[:10],
        "total_return": round(total, 6),
        "annual_return": round(annual, 6),
        "benchmark_return": round(btotal, 6),
        "annual_excess": round(annual - bannual, 6),
        "max_drawdown": round(float((nav / nav.cummax() - 1.0).min()), 6),
        "sharpe": (round(float(daily.mean() / daily.std() * math.sqrt(TRADING_DAYS)), 4)
                   if float(daily.std()) else None),
        "daily_excess_t": None if t is None else round(t, 2),
    }


def yearly(values: pd.Series, bench: pd.Series) -> list[dict]:
    out = []
    for year in sorted({d.year for d in values.index}):
        sv = values[values.index.year == year]
        bv = bench[bench.index.year == year]
        if len(sv) < 2 or len(bv) < 2:
            continue
        idx = sv.index.intersection(bv.index)
        sv, bv = sv.loc[idx], bv.loc[idx]
        if len(sv) < 2:
            continue
        r = float(sv.iloc[-1] / sv.iloc[0] - 1.0)
        b = float(bv.iloc[-1] / bv.iloc[0] - 1.0)
        out.append({"year": str(year), "return": round(r, 6),
                    "benchmark": round(b, 6), "excess": round(r - b, 6)})
    return out


def gate(result, costs: dict) -> tuple[bool, dict]:
    m = result.metrics
    eq = curve(result)
    one_way = (m.get("turnover_daily") or 0.0) / 2.0
    expected = one_way * (float(costs["open_cost"]) + float(costs["close_cost"])) * TRADING_DAYS
    ratio = (m["cost_annualized"] / expected) if expected and m.get("cost_annualized") is not None else None
    # 只有买入、没有卖出的窗口验不了「买+卖」的往返费率；不把这种情况误判成仪器坏。
    cost_ratio_ok = (int(m["sell_count"]) == 0
                     or (ratio is not None and 0.5 <= ratio <= 1.5))
    checks = {
        "curve_start": abs(float(eq.iloc[0]) - float(m["start_value"])) < 1.0,
        "curve_end": abs(float(eq.iloc[-1]) - float(m["end_value"])) < 1.0,
        "gross_ge_net": float(m["gross_return"]) >= float(m["total_return"]) - 1e-9,
        "cost_ratio": cost_ratio_ok,
        "trades": int(m["trade_count"]) > 0,
    }
    return all(checks.values()), {**checks, "cost_ratio_value": None if ratio is None else round(ratio, 3),
                                  "cost_ratio_skipped": int(m["sell_count"]) == 0}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2024-01-02")
    ap.add_argument("--end", default="2026-10-22")
    ap.add_argument("--capital", type=float, default=100_000_000.0)
    ap.add_argument("--benchmark", default="SH510300")
    ap.add_argument("--min-bars", type=int, default=130)
    ap.add_argument("--min-amount", type=float, default=1e8)
    ap.add_argument("--chunk", type=int, default=300)
    ap.add_argument("--entry-rank", type=int, default=50)
    ap.add_argument("--exit-rank", type=int, default=100)
    ap.add_argument("--topk", default="1,10")
    ap.add_argument("--stop-loss", type=float, default=0.0,
                    help="相对买入价的止损，小数（0.08=-8%%）；0=关闭")
    ap.add_argument("--take-profit", type=float, default=0.0,
                    help="相对买入价的止盈，小数（0.10=+10%%）；0=关闭")
    ap.add_argument("--st-file", type=Path, default=ST_FILE)
    ap.add_argument("--out", type=Path, default=OUT_DEFAULT)
    ap.add_argument("--scores-path", type=Path, default=None,
                    help="复用已有分数面板；配合 --reuse-scores")
    ap.add_argument("--reuse-scores", action="store_true")
    ap.add_argument("--prepare-only", action="store_true")
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--limit-symbols", type=int, default=0)
    args = ap.parse_args()

    topks = [int(x) for x in str(args.topk).split(",") if x.strip()]
    if not topks or any(k < 1 for k in topks):
        raise SystemExit("--topk 至少给一个正整数")
    if args.exit_rank < args.entry_rank:
        raise SystemExit("--exit-rank 必须 >= --entry-rank")
    if args.stop_loss < 0 or args.take_profit < 0:
        raise SystemExit("--stop-loss / --take-profit 不能为负")
    if args.scores_path and not args.reuse_scores:
        raise SystemExit("--scores-path 只用于复用，必须同时给 --reuse-scores")

    art = artifacts.load()
    serving.init_qlib()
    days = serving.calendar()
    start, end = _snap_window(days, args.start, args.end)
    warm_beg = days[max(0, days.index(start) - serving.WARMUP_DAYS + 1)]
    print(f"模型 {art.run_id} train_end={art.meta.get('train_end')}；"
          f"窗口 {start}→{end}（warmup {warm_beg}），基准 {args.benchmark}", flush=True)
    print(f"起始内存：可用 {free_gb():.1f} GB，RSS {rss_mb():.0f} MB", flush=True)

    symbols = panel_stocks()
    st_codes = load_st_codes(args.st_file)
    close, amount = load_panel(symbols, days)
    # 板块剔筛与线上同源（serving.BOARD_EXCLUDE）：688/689/300/301 用户没有交易权限，
    # 留在验证票池里，护栏 P25 分位和名次人口都不是线上那份名单，读数就没法对照。
    bad = [c for c in close.columns if serving.board_excluded(c)]
    if bad:
        close = close.drop(columns=bad)
        amount = amount.drop(columns=bad)
    print(f"面板 {close.shape[0]} 天 × {close.shape[1]} 只"
          f"（剔 300/301/688/689 {len(bad)} 只），RSS {rss_mb():.0f} MB", flush=True)

    all_u, st_u, ustats = build_universes(close, amount, start, end, st_codes,
                                          args.min_bars, args.min_amount)
    ustats["board_excluded"] = len(bad)
    print("票池：" + json.dumps(ustats, ensure_ascii=False), flush=True)
    if args.limit_symbols:
        keep = sorted(set(all_u.columns[all_u.any()]))[:args.limit_symbols]
        all_u, st_u = all_u[keep], st_u[keep]
        print(f"冒烟限制到 {len(keep)} 只", flush=True)
    union = sorted(set(all_u.columns[all_u.any()]))
    if not union:
        raise RuntimeError("动态票池是空的")

    close_w = close.loc[warm_beg:end]
    amount_w = amount.loc[warm_beg:end]
    del close, amount
    gc.collect()
    print(f"切到 warmup 窗口后 RSS {rss_mb():.0f} MB，union {len(union)} 只", flush=True)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    score_path = args.scores_path or args.out.with_name(args.out.name + "_scores.parquet")
    pred, sstats = score_union(art, union, warm_beg, end, args.chunk, score_path,
                               args.reuse_scores)
    pred = pred.loc[start:end]
    print(f"分数面板 {len(pred):,} 行，RSS {rss_mb():.0f} MB", flush=True)

    signals, sigstats = {}, {}
    for name, elig in (("st", st_u), ("all", all_u)):
        sig, st = build_signal(pred, elig, close_w, amount_w, name)
        signals[name] = sig
        sigstats[name] = st
        sig.rename("score").to_frame().to_parquet(
            args.out.with_name(args.out.name + f"_signal_{name}.parquet"))
    del pred
    gc.collect()

    if args.prepare_only:
        meta = {"model": art.describe(), "window": {"start": start, "end": end,
                "warm_beg": warm_beg, "requested_start": args.start,
                "requested_end": args.end}, "universe": ustats,
                "score": sstats, "signals": sigstats,
                "peak_rss_mb": round(rss_mb(), 0)}
        args.out.with_name(args.out.name + "_prepare.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        print("prepare-only 完成；信号已落盘", flush=True)
        return 0

    bench = benchmark_curve(args.benchmark, start, end)
    base_costs = dict(bt.EXCHANGE_KWARGS)
    stress_costs = {**base_costs, "open_cost": 0.0015, "close_cost": 0.0025}
    cost_modes = {"base": base_costs, "stress": stress_costs}
    arms: dict[str, dict] = {}
    curves: dict[str, pd.Series] = {"bench": bench / bench.iloc[0]}
    trades: list[dict] = []

    for mode in ("st", "all"):
        for topk in topks:
            for cost_name in ("base", "stress"):
                if mode == "all" and cost_name == "stress":
                    continue  # 敏感性只跑基准成本
                label = f"{mode}_top{topk}_{cost_name}"
                result = run_arm(signals[mode], union, start, end, args.capital,
                                 args.benchmark, topk, args.entry_rank, args.exit_rank,
                                 cost_modes[cost_name], label,
                                 stop_loss=args.stop_loss, take_profit=args.take_profit)
                eq = curve(result)
                curves[label] = eq
                gok, ginfo = gate(result, cost_modes[cost_name])
                rec = {
                    "label": label, "mode": mode, "topk": topk, "cost": cost_name,
                    "params": {"entry_rank": args.entry_rank, "exit_rank": args.exit_rank,
                               "capital": args.capital, "benchmark": args.benchmark,
                               "stop_loss_pct": args.stop_loss,
                               "take_profit_pct": args.take_profit},
                    "metrics": result.metrics,
                    "gate": ginfo, "gate_ok": gok,
                    "yearly": yearly(eq, bench),
                    "trailing_1y": subperiod(eq, bench, "2025-10-23"),
                    "full": subperiod(eq, bench),
                    "executability": result.executability,
                }
                arms[label] = rec
                for t in result.trades:
                    trades.append({"arm": label, **t})
                print(f"  {label}: 年化 {result.metrics['annual_return']:+.2%}  "
                      f"超额 {result.metrics['annual_excess']:+.2%}  "
                      f"回撤 {result.metrics['max_drawdown']:.2%}  "
                      f"换手 {(result.metrics.get('turnover_daily') or 0):.2%}/日  "
                      f"年成本 {(result.metrics.get('cost_annualized') or 0):.2%}", flush=True)

    pd.DataFrame(curves).to_parquet(args.out.with_name(args.out.name + "_curves.parquet"))
    pd.DataFrame(trades).to_parquet(args.out.with_name(args.out.name + "_trades.parquet"))

    def _arm_ok(prefix: str) -> bool:
        a = arms.get(f"{prefix}_base")
        s = arms.get(f"{prefix}_stress")
        if not a or not s or not a["gate_ok"] or not s["gate_ok"]:
            return False
        years = [y for y in a["yearly"] if y["year"] in ("2024", "2025", "2026")]
        return ((a["metrics"].get("annual_excess") or -1) > 0
                and (s["metrics"].get("annual_excess") or -1) > 0
                and (a["trailing_1y"].get("annual_excess") or -1) > 0
                and sum(y["excess"] > 0 for y in years) >= 2)

    a_ok, b_ok = _arm_ok("st_top1"), _arm_ok("st_top10")
    verdict = ("两臂都过线（仍需 3~6 个月影子盘，不直接实盘）" if a_ok and b_ok
               else "单票臂过线，列表臂不过（仍需影子盘，不能拿列表背书）" if a_ok
               else "未证明（不实盘）")
    meta = {
        "protocol": "stock_selector/docs/qlib全市场topk验证_20261025.md",
        "model": art.describe(),
        "window": {"start": start, "end": end, "warm_beg": warm_beg,
                   "requested_start": args.start, "requested_end": args.end},
        "universe": ustats,
        "score": sstats,
        "signals": sigstats,
        "costs": {"base": base_costs, "stress": stress_costs},
        "stops": {"stop_loss_pct": args.stop_loss,
                  "take_profit_pct": args.take_profit,
                  "note": "按相对买入价、当日收盘判断并按同一收盘成交；日频近似，盘中/跳空未建模"},
        "arms": arms,
        "verdict": {"top1_pass": a_ok, "top10_pass": b_ok, "text": verdict},
        "peak_rss_mb": round(rss_mb(), 0),
    }
    args.out.with_name(args.out.name + "_metrics.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("\n=== 裁决 ===")
    print(f"topk=1 主臂：{'过线' if a_ok else '不过'}")
    print(f"topk=10 对照：{'过线' if b_ok else '不过'}")
    print(f"结论：{verdict}")
    print(f"产物：{args.out.with_name(args.out.name + '_metrics.json')}")
    print(f"峰值 RSS：{rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
