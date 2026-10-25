"""MA5 趋势闸门 AB：只买「MA5 向上」的票，卖出/持有纪律一字不动。

用户命题（2026-10-25）：「趋势向下买了就一直割肉；买趋势向上的票，回撤应该小」。
本脚本把这句话变成一次可读数的实验，规则**跑之前写死，读完不改**。

预注册：
- 闸门定义（信号日 F = 成交日 T−1，与分数同一信息集，无未来函数）：
  `close_F > MA5_F` 且 `MA5_F > MA5_{F−1}`（严格向上）；MA5 不足 5 根或行情缺失 = 不符合。
  价格源 = 回测同一份 qlib bin close（/data/work/qlib_data/cn/features）。
- B 臂（默认，"全场换人版"）：top-50 内分数最高且闸门开的那只才买；
  top-50 里没有一只开门就不买。
- C 臂（--head-only，2026-10-25 追问后补注册，"不动头名版"）：
  allowed 只放「当日模型头名且闸门开」那一格；头名不满足就当天不买（空仓等），**不换人**。
  直接检验「趋势向上的才有买的价值」这句命题，且保持「买最高分一只」的产品口径。
- 两臂都只用既有实验旋钮 `BandHoldStrategy.entry_gate`（批次 O 留下的），卖出/持有纪律一字不动。
- 对照 = 2026-10-25 主验证已落盘的 st_top1_base / st_top1_stress（不重跑）。
- 其余对齐主验证：信号复用 fullmarket_validation_20261025_signal_st.parquet
  （动态票池 + 两条名单护栏的产物），窗口 2024-01-02→2026-10-22，
  成本基准 + 滑点压力两档与主验证完全相同。

用法：
    python3 -u scripts/qlib_ma5_gate_ab.py --smoke          # 只验管线
    python3 -u scripts/qlib_ma5_gate_ab.py                  # topk=1，base+stress
    python3 -u scripts/qlib_ma5_gate_ab.py --topk 10        # 多票对照
    python3 -u scripts/qlib_ma5_gate_ab.py --head-only      # C 臂：头名 MA5 向上才买，否则空仓等
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
for p in (str(REPO), str(SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

import qlib_fullmarket_validation as fv  # noqa: E402
import stock_selector_qlib.backtest as bt  # noqa: E402
from stock_selector_qlib import serving  # noqa: E402
from stock_selector_qlib.backtest import _snap_window, run_qlib_backtest  # noqa: E402

OUT = REPO / "data" / "qlib" / "ma5gate_20261025"
SIGNAL = REPO / "data" / "qlib" / "fullmarket_validation_20261025_signal_st.parquet"
CONTROL = REPO / "data" / "qlib" / "fullmarket_validation_20261025_metrics.json"
CONTROL_CURVES = REPO / "data" / "qlib" / "fullmarket_validation_20261025_curves.parquet"
TRADING_DAYS = 243


def ma5_ok(close: pd.DataFrame) -> pd.DataFrame:
    """逐日「MA5 向上」布尔宽表：close 在 MA5 上且 MA5 比昨天高；不足 5 根/缺行情 = False。"""
    ma5 = close.rolling(5, min_periods=5).mean()
    return (close > ma5) & (ma5 > ma5.shift(1))


def gate_series(ok: pd.DataFrame, sig: pd.Series) -> pd.Series:
    """把宽表闸门按信号 (datetime, instrument) 逐格取值；1.0=能买，0.0=不能买。"""
    days = sig.index.get_level_values("datetime")
    syms = sig.index.get_level_values("instrument")
    ok_np = ok.to_numpy()
    pos_d = ok.index.get_indexer(days)
    pos_s = ok.columns.get_indexer(syms)
    vals = np.zeros(len(sig), dtype=bool)
    good = (pos_d >= 0) & (pos_s >= 0)
    vals[good] = ok_np[pos_d[good], pos_s[good]]
    miss = int((~good).sum())
    if miss:
        print(f"闸门缺格 {miss:,} 行（面板没有该票/该日）→ 当作不符合", flush=True)
    return pd.Series(np.where(vals, 1.0, 0.0), index=sig.index, name="gate")


def run_arm(sig: pd.Series, pool: list[str], params: dict, costs: dict,
            label: str, entry_gate: pd.Series):
    old_costs, old_pool, old_source = bt.EXCHANGE_KWARGS, serving.score_pool, serving.POOL_SOURCE
    bt.EXCHANGE_KWARGS = dict(costs)
    serving.score_pool = lambda _art: pool
    serving.POOL_SOURCE = "universe"
    args = {**params, "entry_gate": entry_gate}
    try:
        t0 = time.time()
        result = run_qlib_backtest(args, signal=sig)
        print(f"  撮合 {label}：{time.time() - t0:.0f}s，净值 {result.metrics['total_return']:+.2%}",
              flush=True)
        return result
    finally:
        bt.EXCHANGE_KWARGS, serving.score_pool, serving.POOL_SOURCE = old_costs, old_pool, old_source


def fmt(v, pct=True):
    if v is None:
        return "—"
    return f"{v:+.2%}" if pct else f"{v:.2%}"


def show(label, m):
    return (f"{label:<14} 年化 {fmt(m['annual_return'])}  超额 {fmt(m['annual_excess'])}  "
            f"回撤 {fmt(m['max_drawdown'])}  换手 {fmt(m.get('turnover_daily') or 0, pct=False)}/日  "
            f"年成本 {fmt(m.get('cost_annualized') or 0, pct=False)}")


def main() -> int:
    ap = argparse.ArgumentParser(description="MA5 趋势闸门 AB（只挡买入）")
    ap.add_argument("--start", default="2024-01-02")
    ap.add_argument("--end", default="2026-10-22")
    ap.add_argument("--capital", type=float, default=100_000_000.0)
    ap.add_argument("--benchmark", default="SH510300")
    ap.add_argument("--entry-rank", type=int, default=50)
    ap.add_argument("--exit-rank", type=int, default=100)
    ap.add_argument("--topk", type=int, default=1)
    ap.add_argument("--signal", type=Path, default=SIGNAL)
    ap.add_argument("--control", type=Path, default=CONTROL)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--smoke", action="store_true", help="只验管线：2026-07-01→2026-09-30")
    ap.add_argument("--head-only", action="store_true",
                    help="C 臂：只买「MA5 向上的模型头名」，头名不满足就当天不买（空仓等），不换人")
    args = ap.parse_args()
    if args.exit_rank < args.entry_rank:
        raise SystemExit("--exit-rank 必须 >= --entry-rank")
    if args.smoke:
        args.start, args.end = "2026-07-01", "2026-09-30"

    serving.init_qlib()
    days = serving.calendar()
    start, end = _snap_window(days, args.start, args.end)
    sig = pd.read_parquet(args.signal)["score"].loc[start:end]
    if sig.empty:
        raise RuntimeError(f"{start}→{end} 信号一行都没有：{args.signal}")
    pool = sorted(set(sig.index.get_level_values("instrument")))
    print(f"窗口 {start}→{end}，信号 {len(sig):,} 行、{len(pool)} 只，topk={args.topk}", flush=True)

    print("读 close 面板算 MA5 …", flush=True)
    close, _ = fv.load_panel(pool, days)
    ok = ma5_ok(close)
    del close
    gate = gate_series(ok, sig)

    # 诊断：闸门把多少天的头名/多少比例的候选拦下了（解释读数用，不是裁决）
    frame = sig.reset_index()
    top_pos = frame.groupby("datetime")["score"].idxmax().to_numpy()
    top1_vals = gate.to_numpy()[top_pos]
    top1_blocked = int((top1_vals < 0.5).sum())
    all_days = int(sig.index.get_level_values("datetime").nunique())
    row_rate = float((gate < 0.5).mean())
    print(f"闸门拦截：头名被拦 {top1_blocked}/{all_days} 天，候选行 {row_rate:.1%} 被拦", flush=True)

    variant = "gatehead" if args.head_only else "gate"
    if args.head_only:
        # 头名唯一闸门：当天只有「模型头名且闸门开」那一格可买；头名不满足就空仓等，不换人
        head_gate = np.zeros(len(sig), dtype=float)
        head_gate[top_pos] = top1_vals
        gate = pd.Series(head_gate, index=sig.index, name="gate")
        print("C 臂（head-only）：只有「MA5 向上的模型头名」当天可买，其余一律不买", flush=True)

    base = dict(bt.EXCHANGE_KWARGS)
    stress = {**base, "open_cost": 0.0015, "close_cost": 0.0025}
    params = {"start": start, "end": end, "capital": args.capital, "engine": "qlib",
              "benchmark": args.benchmark, "hold_rule": "band", "topk": args.topk,
              "entry_rank": args.entry_rank, "exit_rank": args.exit_rank,
              "stop_loss_pct": 0.0, "take_profit_pct": 0.0}
    bench = fv.benchmark_curve(args.benchmark, start, end)
    curves = {"bench": bench / bench.iloc[0]}
    arms, trades_n = {}, 0
    for cost_name, costs in (("base", base), ("stress", stress)):
        label = f"{variant}_top{args.topk}_{cost_name}"
        result = run_arm(sig, pool, params, costs, label, gate)
        eq = fv.curve(result)
        curves[label] = eq
        gok, ginfo = fv.gate(result, costs)
        arms[label] = {"label": label, "topk": args.topk, "cost": cost_name,
                       "metrics": result.metrics, "gate": ginfo, "gate_ok": gok,
                       "yearly": fv.yearly(eq, bench),
                       "trailing_1y": fv.subperiod(eq, bench, "2025-10-23"),
                       "full": fv.subperiod(eq, bench)}
        trades_n += len(result.trades)
        print("  " + show(label + "（MA5闸门）", result.metrics), flush=True)

    control = json.loads(args.control.read_text(encoding="utf-8"))
    print("\n=== 对照 vs MA5 闸门（同窗口/同信号/同成本） ===")
    for cost_name in ("base", "stress"):
        c = control["arms"].get(f"st_top{args.topk}_{cost_name}")
        if c:
            print("  " + show(f"对照_{cost_name}", c["metrics"]))
        a = arms.get(f"{variant}_top{args.topk}_{cost_name}")
        if a:
            print("  " + show(f"闸门_{cost_name}", a["metrics"]))

    ctrl_curves = pd.read_parquet(CONTROL_CURVES)
    tag = f"st_top{args.topk}_"
    for cost_name in ("base", "stress"):
        col = tag + cost_name
        if col in ctrl_curves.columns:
            curves[f"ctrl_{col}"] = ctrl_curves[col]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    tag_out = args.out.name + ("_headonly" if args.head_only else "")
    pd.DataFrame(curves).to_parquet(args.out.parent / (tag_out + f"_curves.parquet"))
    meta = {
        "protocol": "MA5 趋势闸门 AB（只挡买入；定义/窗口/对照在脚本头预注册）",
        "variant": ("head_only：只有「MA5 向上的模型头名」当天可买，头名不满足就空仓等"
                    if args.head_only else "all_eligible：top-50 内最高分的 MA5 向上票才买"),
        "gate_def": {"close_above_ma5": True, "ma5_rising_strict": True,
                      "min_bars": 5, "price_source": "qlib bin close.day.bin（与回测同源）",
                      "decision_day": "信号日 F=成交日 T−1，与分数同一信息集"},
        "only_side": "entry_gate 只挡买入；卖出/持有纪律不变（band 50/100）",
        "signal": str(args.signal),
        "window": {"start": start, "end": end, "requested": [args.start, args.end]},
        "costs": {"base": base, "stress": stress},
        "blocked": {"top1_days": top1_blocked, "all_days": all_days,
                    "top1_rate": round(top1_blocked / max(all_days, 1), 4),
                    "row_rate": round(row_rate, 4)},
        "arms": arms,
        "control_file": str(args.control),
        "control_arms": {k: {"metrics": v["metrics"], "yearly": v["yearly"],
                             "trailing_1y": v.get("trailing_1y")}
                         for k, v in control["arms"].items()
                         if k in (f"st_top{args.topk}_base", f"st_top{args.topk}_stress")},
        "trades_dumped": 0, "trade_count_total": trades_n,
    }
    out_json = args.out.parent / (tag_out + f"_top{args.topk}_metrics.json")
    out_json.write_text(json.dumps(meta, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
    print(f"\n产物：{out_json}")
    print("读数仅研究对照，不是实盘背书。", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
