"""名单护栏四臂对照：同一份分数、同一套撮合，只在名单上「剔掉谁」。

用法（正式读数，与批次 Q 同池同窗）：
    python3 -u scripts/qlib_guardrail_ab.py
冒烟（验跑得通，窗口 4 个月）：
    python3 -u scripts/qlib_guardrail_ab.py --start 2026-06-01 --end 2026-10-15

口径照 §10/§21 的 A1 band 逐字：进 50 / 出 100 / 持 10，T 日收盘成交、读 T−1 分数，
买 0.05%/卖 0.15%、最低 5 元，±9.5% 挡单，账户 1 亿，基准 SH510300。
四臂共用的只有分数面板与行情；**臂与臂之间唯一的差别是把哪些「日·票」从分数里删掉**，
删掉的票当天既进不了榜、也让正在持有的仓位跌出 band 被卖——这就是线上「不进名单」的语义。

判据来自 stock_selector_qlib/guardrails.py（与 serving 同一份定义，不分两套口径）。
"""
from __future__ import annotations

import argparse
import math
import resource
import sys
import time
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from stock_selector_qlib import artifacts, guardrails, serving  # noqa: E402
from stock_selector_qlib.backtest import _snap_window, run_qlib_backtest, score_panel  # noqa: E402

BAND = {"hold_rule": "band", "topk": 10, "entry_rank": 50, "exit_rank": 100}
ARMS = {
    "A": "现名单（不加护栏）",
    "B": "剔薄流动（20 日均额 < 当日票池 P25）",
    "C": "剔妖痕浅跌（拉升后回撤 ≥20% 但 20 日跌幅 <15%）",
    "D": "B + C 都剔",
}


def price_frames(pool: list[str], beg: str, end: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """[beg, end] 的收盘价/成交额宽表（元）。护栏要往回看 91 根，所以取数窗口先往前挪。"""
    import qlib.data

    warm = max(guardrails.SPIKE_SPAN, guardrails.AMT_WINDOW) + guardrails.SPIKE_STEP
    days = serving.calendar()
    start = days[max(0, days.index(beg) - warm)]
    frame = qlib.data.D.features(list(pool), ["$close", "$amount"], start_time=start, end_time=end)
    out = {}
    for field in ("$close", "$amount"):
        wide = frame[field].unstack("instrument")
        wide.index = pd.to_datetime(wide.index)
        out[field] = wide.sort_index()
    return out["$close"], out["$amount"]


def drop_blocked(panel: pd.Series, block: pd.DataFrame) -> pd.Series:
    """把被判护栏的「日·票」从分数里删掉。

    对不齐就报错，不当「没人被关」：日期索引口径错一位的话，护栏会静默失效，
    读数看起来完全正常（这种坑在面板链上已经踩过一次）。
    """
    flag = block.stack()
    hit = flag.reindex(panel.index)
    missing = int(hit.isna().sum())
    if missing > 0.02 * len(panel):
        raise RuntimeError(f"护栏面板与分数面板对不上：{missing}/{len(panel)} 行没有判定"
                           f"（护栏日期范围 {block.index.min()}→{block.index.max()}，"
                           f"分数 {panel.index.get_level_values('datetime').min()}→"
                           f"{panel.index.get_level_values('datetime').max()}）")
    return panel[~hit.fillna(False).astype(bool)]


def _incidence(benches: dict, beg: str, end: str) -> None:
    """撮合之前先看名单本身动了谁：A 的前 30 名被剔了多少、被剔那批后来涨多少。

    收益口径与撮合对齐：**成交日（分数日的下一根 K 线）收盘入场，持有 10 个交易日**，
    超额 = 减当日全池均值（市场中性）。这样这里报的和撮合臂看的是同一段钱。
    """
    base = benches["A"]
    codes = sorted(set(base.index.get_level_values("instrument")))
    close = _wide(codes, "$close", beg, end)
    fwd = (close.shift(-11) / close.shift(-1) - 1.0).stack()
    frame = base.to_frame("score")
    frame["fwd"] = fwd.reindex(frame.index)
    if frame["fwd"].isna().mean() > 0.2:
        raise RuntimeError(f"收益面板与分数面板对不上：{frame.fwd.isna().sum()}/{len(frame)} 行没有收益")
    frame["rk"] = frame.groupby(level="datetime")["score"].rank(ascending=False, method="first")
    frame["excess"] = frame["fwd"] - frame.groupby(level="datetime")["fwd"].transform("mean")
    top = frame[frame.rk <= 30]
    first = top[top.rk == 1]
    days_n = top.index.get_level_values("datetime").nunique()
    print(f"\n=== 名单改动（A 的前 30 名，共 {len(top)} 个「日·票」位 / {days_n} 天）===")
    print(f"{'臂':<4}{'A榜内被剔':>10}{'占比':>8}{'被剔那批超额':>13}{'t':>6}"
          f"{'留下那批超额':>13}{'t':>6}{'第一名被换的天数':>15}")
    for name in ("B", "C", "D"):
        keep = set(benches[name].index)
        hit = top.index.isin(keep)
        gone, stay = top[~hit], top[hit]
        moved = int((~first.index.isin(keep)).sum())
        print(f"{name:<4}{len(gone):>10}{len(gone) / len(top):>8.1%}"
              f"{_mean(gone.excess) * 100:>12.2f}pp{_tt(gone.excess):>6.1f}"
              f"{_mean(stay.excess) * 100:>12.2f}pp{_tt(stay.excess):>6.1f}"
              f"{moved:>15}")
    print("（被剔那批明显更差 = 护栏砍对了；两者差不多 = 只是凭空加换手）")


def _mean(s: pd.Series) -> float:
    return float(s.mean()) if len(s) else float("nan")


def _tt(s: pd.Series) -> float:
    if len(s) < 2:
        return float("nan")
    return float(s.mean()) / float(s.std(ddof=1)) * math.sqrt(len(s))


def _wide(codes: list[str], field: str, beg: str, end: str) -> pd.DataFrame:
    import qlib.data

    frame = qlib.data.D.features(list(codes), [field], start_time=beg, end_time=end)[field]
    wide = frame.unstack("instrument")
    wide.index = pd.to_datetime(wide.index)
    return wide.sort_index()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--start", default="2024-01-02")
    ap.add_argument("--end", default="2026-10-15")
    ap.add_argument("--capital", type=float, default=100_000_000.0)
    ap.add_argument("--benchmark", default="SH510300")
    ap.add_argument("--arms", default="A,B,C,D")
    ap.add_argument("--panel-only", action="store_true",
                    help="只打印名单改动（不跑撮合）：先看护栏砍对了没有，再决定要不要花几分钟撮合")
    args = ap.parse_args()

    names = [a.strip().upper() for a in args.arms.split(",") if a.strip()]
    bad = [a for a in names if a not in ARMS]
    if bad:
        raise SystemExit(f"不认识这些臂：{bad}")

    art = artifacts.load()
    # 与线上同一票池来源：QLIB_POOL_SOURCE=universe 时这里才是 2,829 只，不是训练池 300 只。
    pool = serving.score_pool(art)
    print(f"模型产物：{art.describe()}  票池 {len(pool)} 只（source={serving.POOL_SOURCE}）")
    serving.init_qlib()
    days = serving.calendar()
    beg, end = _snap_window(days, args.start, args.end)

    t0 = time.time()
    panel = score_panel(art, beg, end, pool=pool)
    print(f"窗口 {beg}→{end}（{len(panel.index.get_level_values('datetime').unique())} 个交易日，"
          f"{len(panel)} 行打分），打分耗时 {time.time() - t0:.0f} 秒")

    closes, amounts = price_frames(pool, beg, end)
    thin = guardrails.thin_liquidity(amounts)
    yao = guardrails.spike_fade(closes)
    both = guardrails.blocked(closes, amounts)
    benches = {"A": panel, "B": drop_blocked(panel, thin),
               "C": drop_blocked(panel, yao), "D": drop_blocked(panel, both)}
    _incidence(benches, beg, end)
    if args.panel_only:
        print("\n（--panel-only：只看了名单改动，没跑撮合）")
        return 0

    bench_nav = _benchmark_curve(args.benchmark, beg, end)
    rows, curves = [], {}
    for name in names:
        t1 = time.time()
        result = run_qlib_backtest({"start": args.start, "end": args.end, "capital": args.capital,
                                    "engine": "qlib", "benchmark": args.benchmark, **BAND},
                                   signal=benches[name])
        m = dict(result.metrics)
        m["one_way_turnover"] = (m["turnover_daily"] or 0.0) / 2.0
        m["rows"] = int(len(benches[name]))
        curves[name] = pd.Series({pd.Timestamp(e["date"]): float(e["total_value"])
                                  for e in result.equity}).sort_index()
        rows.append((name, m))
        print(f"  {name} 撮合完成（{time.time() - t1:.0f} 秒），分数 {m['rows']} 行，卖出 {m['sell_count']} 笔")

    print("\n=== 净值读数（已扣成本；同池同窗同成本，只差名单剔谁）===")
    for name, m in rows:
        print(f"{name} {ARMS[name]}")
        print(f"    总收益 {m['total_return']:+.2%}  年化 {m['annual_return']:+.2%}  "
              f"年化超额 {m['annual_excess']:+.2%}  最大回撤 {m['max_drawdown']:.2%}")
        print(f"    毛利 {m['gross_return']:+.2%}（差的就是手续费）  单边换手 {m['one_way_turnover']:.2%}/日  "
              f"年成本 {m['cost_annualized']:.2%}  平均持有 {m['avg_hold_days']} 天")
        print(f"    卖出 {m['sell_count']} 笔  胜率 {m['win_rate']}  盈亏比 {m['payoff_ratio']}  "
              f"夏普 {m['sharpe']}")

    _significance(curves, bench_nav)
    _ic(benches, names, beg, end)
    dest = REPO / "data" / "qlib" / "batch_R_curves.parquet"
    pd.DataFrame(curves | {"bench": bench_nav}).to_parquet(dest)
    print(f"\n净值曲线落盘（本地文件，没写 Mongo）：{dest}")
    print(f"峰值 RSS：{resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0:.0f} MB")
    return 0


def _t(daily: pd.Series) -> float:
    std = float(daily.std())
    return float(daily.mean()) / std * math.sqrt(len(daily)) if std else float("nan")


def _significance(curves: dict, bench_nav: pd.Series) -> None:
    """与 A 臂的逐日配对差。标签 T+1→T+10 重叠 9 天，t 要 ÷√10 才接近真实读数（括号里给）。"""
    print("\n=== 与 A 臂的配对日净差（扣成本净值）===")
    daily = {k: v.pct_change().dropna() for k, v in curves.items()}
    if "A" not in daily:
        print("  没跑 A 臂，配对读数跳过")
        return
    bench_daily = bench_nav.pct_change().dropna()
    for name in daily:
        excess = (daily[name] - bench_daily).dropna()
        print(f"  {name}：自身日净收益 t={_t(daily[name]):.2f}  vs 基准超额 t={_t(excess):.2f}"
              f"（÷√10 → {_t(excess) / math.sqrt(10):.2f}）")
    for name in ("B", "C", "D"):
        if name not in daily:
            continue
        pair = (daily[name] - daily["A"]).dropna()
        print(f"  {name}−A 配对：均值 {pair.mean() * 1e4:+.2f} bp/日  年化 {pair.mean() * 243:+.2%}  "
              f"t={_t(pair):.2f}（÷√10 → {_t(pair) / math.sqrt(10):.2f}）  n={len(pair)}")


def _ic(benches: dict, names: list[str], beg: str, end: str) -> None:
    """名单改动对排序能力的影响：每日 Spearman(分数, T+10 收益)。剔票会动名次，所以要一起看。"""
    import qlib.data

    codes_all = sorted(set(benches["A"].index.get_level_values("instrument")))
    frame = qlib.data.D.features(codes_all, ["$close"], start_time=beg, end_time=end)
    wide = frame["$close"].unstack("instrument")
    wide.index = pd.to_datetime(wide.index)
    fwd = wide.shift(-10) / wide - 1.0
    n_days = benches["A"].index.get_level_values("datetime").nunique()
    print("\n=== 日度 Rank IC（Spearman：当日分数 vs 该票 T+10 毛收益，窗口末尾 10 天算不出）===")
    for name in names:
        series = benches[name]
        day_ic = []
        skipped_days = 0
        for day, g in series.groupby(level="datetime"):
            if day not in fwd.index:
                skipped_days += 1
                continue
            y = fwd.loc[day, g.index.get_level_values("instrument")]
            pair = pd.DataFrame({"s": g.to_numpy(), "f": y.to_numpy()}).dropna()
            if len(pair) >= 30:
                day_ic.append(pair.s.corr(pair.f, method="spearman"))
        if skipped_days > 0.05 * n_days:
            raise RuntimeError(f"IC 读数作废：{skipped_days}/{n_days} 天在收益面板里找不到对应日期")
        ic = pd.Series(day_ic).dropna()
        print(f"  {name}：均值 {ic.mean():+.4f}  日 t={_t(ic):.2f}  n={len(ic)} 天")


def _benchmark_curve(benchmark: str, beg: str, end: str) -> pd.Series:
    import qlib.data

    frame = qlib.data.D.features([benchmark], ["$close"], start_time=beg, end_time=end)["$close"]
    if isinstance(frame.index, pd.MultiIndex):
        frame = frame.droplevel("instrument")
    frame.index = pd.to_datetime(frame.index)
    closes = frame.sort_index().dropna()
    if closes.empty:
        raise RuntimeError(f"基准 {benchmark} 在窗口内没有行情")
    return closes / closes.iloc[0]


if __name__ == "__main__":
    sys.exit(main())
