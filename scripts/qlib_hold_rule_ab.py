"""批次 G 四臂 AB：同一份分数、同一套撮合，只换「什么时候卖」的纪律。

用法（正式读数，2023-01-03→2026-10-15）：
    python3 -u scripts/qlib_hold_rule_ab.py
先冒烟（只验跑得通，窗口 4 个月）：
    python3 -u scripts/qlib_hold_rule_ab.py --start 2026-06-01 --end 2026-10-15 --arms A1,A3

四臂的口径、主臂怎么选、裁决线全部预注册在
`stock_selector/docs/qlib路线数据口径与验收门槛.md` §10，这里只负责跑与打印。

两件事和单次回测不一样：
1. **打分只算一次**（因子面板是内存大头），四臂复用同一个 `signal`，所以峰值 RSS ≈ 单臂；
2. **仪器门没过就不打印收益**——换手/成本/平均持有天数对不上预注册区间说明实现走样，
   这时候看收益只会把一个坏仪器读数记成结论。
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

from stock_selector_qlib import artifacts, serving  # noqa: E402
from stock_selector_qlib.backtest import _snap_window, run_qlib_backtest, score_panel  # noqa: E402

# 预注册的四臂（docs §10 G2）。A1 是主臂，裁决以它为准。
ARMS = {
    "A1": {"label": "band 进50/出100/持10（主臂）", "hold_rule": "band",
           "topk": 10, "entry_rank": 50, "exit_rank": 100},
    "A2": {"label": "band 进50/出150/持10", "hold_rule": "band",
           "topk": 10, "entry_rank": 50, "exit_rank": 150},
    "A3": {"label": "band 进50/出50/持10（同只数对照）", "hold_rule": "band",
           "topk": 10, "entry_rank": 50, "exit_rank": 50},
    "A4": {"label": "TopkDropout 持30/每天换5（§8 初版）", "hold_rule": "dropout",
           "topk": 30, "n_drop": 5},
}

# 主臂的仪器门（docs §10 G3 第 1 条）。单边换手按 qlib `turnover` 折半算：
# 那一列是买额+卖额之和除以昨净值，TopkDropout 强制换 5/30 时它报 ~33%，对应单边 16.7%。
GATE = {"one_way_turnover": (0.12, 0.17), "avg_hold_days": (5.0, 9.0)}
# 成本改成自洽校验而不是绝对上限：预期年成本 = 单边换手 ×（买费率 + 卖费率）× 年交易日。
# 预注册时把它算成 3.5pp（把双边费率当单边），实测约 6.9pp，是算术错、不是实现错，
# 所以在读任何收益之前换成这个不依赖窗口、能同时验算 qlib 记账的式子。
FEE_ROUND_TRIP = 0.0005 + 0.0015


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--start", default="2023-01-03")
    ap.add_argument("--end", default="2026-10-15")
    ap.add_argument("--capital", type=float, default=100_000_000.0)
    ap.add_argument("--benchmark", default="SH510300")
    ap.add_argument("--arms", default="A1,A2,A3,A4", help="逗号分隔，如 A1,A3")
    args = ap.parse_args()

    names = [a.strip().upper() for a in args.arms.split(",") if a.strip()]
    unknown = [a for a in names if a not in ARMS]
    if unknown:
        raise SystemExit(f"不认识这些臂：{unknown}（可选 {list(ARMS)}）")

    art = artifacts.load()
    print(f"模型产物：{art.describe()}  票池 {len(art.pool)} 只")
    serving.init_qlib()
    days = serving.calendar()
    beg, end = _snap_window(days, args.start, args.end)

    t0 = time.time()
    panel = score_panel(art, beg, end)
    print(f"窗口 {beg}→{end}（{len(panel.index.get_level_values('datetime').unique())} 个交易日，"
          f"{len(panel)} 行打分），打分耗时 {time.time() - t0:.0f} 秒")

    bench_nav = _benchmark_curve(args.benchmark, beg, end, days)
    rows, gates, curves = [], {}, {}
    for name in names:
        spec = ARMS[name]
        params = {"start": args.start, "end": args.end, "capital": args.capital,
                  "engine": "qlib", "benchmark": args.benchmark, **spec}
        t1 = time.time()
        result = run_qlib_backtest(params, signal=panel)
        m = dict(result.metrics)
        m["one_way_turnover"] = (m["turnover_daily"] or 0.0) / 2.0
        m["yearly"] = _yearly(result.equity, bench_nav)
        curves[name] = pd.Series({pd.Timestamp(e["date"]): float(e["total_value"])
                                  for e in result.equity}).sort_index()
        rows.append((name, spec["label"], m))
        gates[name] = m
        print(f"  {name} 撮合完成，耗时 {time.time() - t1:.0f} 秒，"
              f"卖出 {m['sell_count']} 笔，成交 {m['trade_count']} 笔")

    if "A1" in gates:
        ok, why = _check_gate(gates["A1"])
        print("\n=== 仪器门（A1，docs §10 G3 第 1 条）===")
        for line, passed in why:
            print(f"  {'过' if passed else '不过'}  {line}")
        if not ok:
            print("读数作废：主臂的换手/成本/持有天数没落在预注册区间，不打印收益。"
                  "先修实现，再看数。")
            return 1

    print("\n=== 净值口径（已扣成本）===")
    head = ("臂", "纪律", "总收益", "年化", "年化超额", "最大回撤", "单边换手/日",
            "年成本", "平均持有", "卖出笔", "胜率", "盈亏比", "毛利")
    print("  ".join(head))
    for name, label, m in rows:
        print(f"{name} {label}")
        print(f"    总收益 {m['total_return']:+.2%}  年化 {m['annual_return']:+.2%}  "
              f"年化超额 {m['annual_excess']:+.2%}  最大回撤 {m['max_drawdown']:.2%}")
        print(f"    毛利 {m['gross_return']:+.2%}（差的就是手续费）  单边换手 {m['one_way_turnover']:.2%}/日  "
              f"年成本 {m['cost_annualized']:.2%}  平均持有 {m['avg_hold_days']} 天")
        print(f"    卖出 {m['sell_count']} 笔  胜率 {m['win_rate']}  盈亏比 {m['payoff_ratio']}  "
              f"期望 {m['expectancy']}  夏普 {m['sharpe']}")
        y = m["yearly"]
        print("    分年净收益/基准/超额：" + "  ".join(
            f"{year}:{r:+.1%}/{b:+.1%}/{r - b:+.1%}" for year, r, b in y))
    _significance(curves, bench_nav)
    dest = REPO / "data" / "qlib" / "batch_G_curves.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(curves | {"bench": bench_nav}).to_parquet(dest)
    print(f"净值曲线落盘（只是本地文件，没写 Mongo）：{dest}")
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    print(f"\n峰值 RSS：{peak:.0f} MB（celery worker 的 cgroup 上限是 3 G）")
    return 0


def _check_gate(m: dict) -> tuple[bool, list[tuple[str, bool]]]:
    lo, hi = GATE["one_way_turnover"]
    expected = (m["one_way_turnover"] or 0.0) * FEE_ROUND_TRIP * 243
    ratio = m["cost_annualized"] / expected if expected else None
    lines = [
        (f"单边换手 {m['one_way_turnover']:.2%} ∈ [{lo:.0%}, {hi:.0%}]",
         m["one_way_turnover"] is not None and lo <= m["one_way_turnover"] <= hi),
        (f"年成本 {m['cost_annualized']:.2%} vs 换手×费率 = {expected:.2%}"
         f"（比值 {ratio:.2f}，需 0.7–1.4）", ratio is not None and 0.7 <= ratio <= 1.4),
    ]
    lo, hi = GATE["avg_hold_days"]
    hold = m["avg_hold_days"]
    lines.append((f"平均持有 {hold} 天 ∈ [{lo:g}, {hi:g}]", hold is not None and lo <= hold <= hi))
    return all(p for _, p in lines), lines


def _benchmark_curve(benchmark: str, beg: str, end: str, days: list[str]) -> pd.Series:
    """基准的收盘价净值序列（起点归一为 1），用来切分年净超额。"""
    import qlib.data

    frame = qlib.data.D.features([benchmark], ["$close"], start_time=beg, end_time=end)["$close"]
    if isinstance(frame.index, pd.MultiIndex):  # (datetime, instrument) 两层，只要日期
        frame = frame.droplevel("instrument")
    frame.index = pd.to_datetime(frame.index)
    closes = frame.sort_index().dropna()
    if closes.empty:
        raise RuntimeError(f"基准 {benchmark} 在窗口内没有行情，超额没法算")
    return closes / closes.iloc[0]


def _yearly(equity: list[dict], bench_nav: pd.Series) -> list[tuple[str, float, float]]:
    """按自然年切：本臂净收益 vs 基准收益（同一年内首末净值相除）。"""
    values = pd.Series({pd.Timestamp(e["date"]): float(e["total_value"]) for e in equity}).sort_index()
    out = []
    for year in sorted({d.year for d in values.index}):
        sv = values[values.index.year == year]
        bv = bench_nav[bench_nav.index.year == year]
        if len(sv) < 2 or len(bv) < 2:
            continue
        ret = float(sv.iloc[-1] / sv.iloc[0] - 1.0)
        bench = float(bv.iloc[-1] / bv.iloc[0] - 1.0)
        out.append((str(year), ret, bench))
    return out



def _t(daily: pd.Series) -> float:
    """日收益均值 t 值（不做任何重叠窗口折算）。"""
    std = float(daily.std())
    return float(daily.mean()) / std * math.sqrt(len(daily)) if std else float("nan")


def _significance(curves: dict, bench_nav: pd.Series) -> None:
    """**预注册里没有这一节**（是看完点估计之后补的），所以它不能用来改判，只用来说明
    「这个差值离噪声有多远」。标签是 T+1→T+5 的前瞻收益，逐日序列一天和下一天重叠 4 天，
    t 值要 ÷√5 才接近真实读数——括号里给的就是折算后的值。"""
    print("\n=== 显著性（事后补的口径，÷√5 折算后仍不到 2 就别当成信号）===")
    daily = {name: values.pct_change().dropna() for name, values in curves.items()}
    bench_daily = bench_nav.pct_change().dropna()
    for name in daily:
        excess = (daily[name] - bench_daily).dropna()
        print(f"  {name}：日净收益 t={_t(daily[name]):.2f}（÷√5 → {_t(daily[name]) / math.sqrt(5):.2f}）  "
              f"vs 基准日超额 t={_t(excess):.2f}（÷√5 → {_t(excess) / math.sqrt(5):.2f}）")
    if "A1" in daily and "A3" in daily:
        pair = (daily["A1"] - daily["A3"]).dropna()
        print(f"  A1−A3 配对日超额：均值 {pair.mean() * 1e4:+.1f} bp/日  "
              f"t={_t(pair):.2f}（÷√5 → {_t(pair) / math.sqrt(5):.2f}）  n={len(pair)}")


if __name__ == "__main__":
    sys.exit(main())
