"""qlib 回测参数扫描：同一份分数面板上找「回撤小、收益高」的组合。

预注册（跑之前写死，读数之后不改）：
- 模型本体不动：分数复用 fullmarket_validation_20261025_signal_st.parquet
  （动态票池 + 两条名单护栏的产物）。换/改模型是另一条训练线，不属于本次探索。
- 扫描维度（都只用已验证过的旋钮）：
  topk ∈ {1,3,5,10}；risk_degree ∈ {0.95,0.7,0.5}；exit_rank ∈ {100,200}（entry_rank 固定 50）。
  hold_rule=band；不带止损止盈、不带 MA5 闸门（2026-10-25 均已验证更差）。
- 成本：全网格跑基准成本一档；对**Pareto 前沿**上的点补跑滑点压力档做稳健性。
- 结论性质：这是同一窗口、同一分数面板上的参数搜索，天然带选参过拟合风险，
  前沿读数只算研究候选，不能当实盘背书；要上线仍按旧规矩：影子盘 3~6 个月。

用法：python3 -u scripts/qlib_pareto_explore.py
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

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

OUT = REPO / "data" / "qlib" / "qlib_pareto_20261025"
SIGNAL = REPO / "data" / "qlib" / "fullmarket_validation_20261025_signal_st.parquet"
START, END = "2024-01-02", "2026-10-22"
TOPKS = (1, 3, 5, 10)
RISKS = (0.95, 0.7, 0.5)
EXITS = (100, 200)
ENTRY = 50

def run_one(sig, pool, params, costs, label):
    old_costs, old_pool, old_source = bt.EXCHANGE_KWARGS, serving.score_pool, serving.POOL_SOURCE
    bt.EXCHANGE_KWARGS = dict(costs)
    serving.score_pool = lambda _art: pool
    serving.POOL_SOURCE = "universe"
    try:
        t0 = time.time()
        result = run_qlib_backtest({**params}, signal=sig)
        m = result.metrics
        print(f"  {label}: 年化 {m['annual_return']:+.2%}  超额 {m['annual_excess']:+.2%}  "
              f"回撤 {m['max_drawdown']:.2%}  换手 {(m.get('turnover_daily') or 0):.2%}/日  "
              f"（{time.time() - t0:.0f}s）", flush=True)
        return result
    finally:
        bt.EXCHANGE_KWARGS, serving.score_pool, serving.POOL_SOURCE = old_costs, old_pool, old_source

def row_of(label, topk, risk, exit_rank, cost, result, bench):
    eq = fv.curve(result)
    m = result.metrics
    return {
        "label": label, "topk": topk, "risk_degree": risk, "exit_rank": exit_rank,
        "cost": cost, "metrics": m, "trades": len(result.trades),
        "yearly": fv.yearly(eq, bench),
        "trailing_1y": fv.subperiod(eq, bench, "2025-10-23"),
        "full": fv.subperiod(eq, bench), "_eq": eq,
    }

def main() -> int:
    ap = argparse.ArgumentParser(description="qlib 回测参数扫描（Pareto 前沿）")
    ap.add_argument("--topk", default=",".join(str(x) for x in TOPKS))
    ap.add_argument("--risks", default=",".join(str(x) for x in RISKS))
    ap.add_argument("--exits", default=",".join(str(x) for x in EXITS))
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    topks = tuple(int(x) for x in args.topk.split(",") if x.strip())
    risks = tuple(float(x) for x in args.risks.split(",") if x.strip())
    exits = tuple(int(x) for x in args.exits.split(",") if x.strip())
    serving.init_qlib()
    days = serving.calendar()
    start, end = _snap_window(days, START, END)
    sig = pd.read_parquet(SIGNAL)["score"].loc[start:end]
    pool = sorted(set(sig.index.get_level_values("instrument")))
    print(f"窗口 {start}→{end}，信号 {len(sig):,} 行、{len(pool)} 只；"
          f"网格 {len(topks) * len(risks) * len(exits)} 臂 + 前沿补压力档", flush=True)

    base = dict(bt.EXCHANGE_KWARGS)
    stress = {**base, "open_cost": 0.0015, "close_cost": 0.0025}
    params = {"start": start, "end": end, "capital": 100_000_000.0, "engine": "qlib",
              "benchmark": "SH510300", "hold_rule": "band", "entry_rank": ENTRY}
    bench = fv.benchmark_curve("SH510300", start, end)
    rows, curves = [], {"bench": bench / bench.iloc[0]}

    for topk in topks:
        for risk in risks:
            for exit_rank in exits:
                label = f"t{topk}_r{risk:.2f}_e{exit_rank}"
                result = run_one(sig, pool, {**params, "topk": topk, "risk_degree": risk,
                                             "exit_rank": exit_rank}, base, label)
                rec = row_of(label, topk, risk, exit_rank, "base", result, bench)
                rows.append(rec)
                curves[label] = rec["_eq"]
                rec.pop("_eq")

    def _num(rec, key, default=0.0):
        return float(rec["metrics"].get(key) or default)

    # Pareto 前沿：年化越高越好、回撤越浅（数值越大）越好；两个都被别臂不差于且至少一维更差 = 被支配
    front = []
    for a in rows:
        for b in rows:
            if b is a:
                continue
            if (_num(b, "annual_return") >= _num(a, "annual_return")
                    and _num(b, "max_drawdown") >= _num(a, "max_drawdown")
                    and (_num(b, "annual_return") > _num(a, "annual_return")
                         or _num(b, "max_drawdown") > _num(a, "max_drawdown"))):
                break
        else:
            front.append(a["label"])

    stress_rows = {}
    for label in front:
        src = next(r for r in rows if r["label"] == label)
        result = run_one(sig, pool, {**params, "topk": src["topk"], "risk_degree": src["risk_degree"],
                                     "exit_rank": src["exit_rank"]}, stress, label + "_stress")
        rec = row_of(label + "_stress", src["topk"], src["risk_degree"], src["exit_rank"],
                     "stress", result, bench)
        stress_rows[label] = rec
        curves[label + "_stress"] = rec["_eq"]
        rec.pop("_eq")

    print("\n=== 全网格（基准成本，按回撤浅→深） ===")
    for rec in sorted(rows, key=lambda r: (-_num(r, "max_drawdown"), -_num(r, "annual_return"))):
        m = rec["metrics"]
        print(f"  {rec['label']:<16} 年化 {_num(rec, 'annual_return'):+.2%}  "
              f"超额 {_num(rec, 'annual_excess'):+.2%}  回撤 {_num(rec, 'max_drawdown'):.2%}  "
              f"收益/回撤比 {_num(rec, 'annual_return') / max(abs(_num(rec, 'max_drawdown')), 1e-9):.2f}  "
              f"年成本 {_num(rec, 'cost_annualized'):.2%}")
    print("\n=== Pareto 前沿（基准成本）+ 压力档 ===")
    for label in front:
        rec = next(r for r in rows if r["label"] == label)
        s = stress_rows.get(label)
        print(f"  前沿 {label:<16} 年化 {_num(rec, 'annual_return'):+.2%} 回撤 {_num(rec, 'max_drawdown'):.2%}"
              + (f"   | 压力后 年化 {_num(s, 'annual_return'):+.2%} 超额 {_num(s, 'annual_excess'):+.2%} "
                 f"回撤 {_num(s, 'max_drawdown'):.2%}" if s else ""))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    arm_dump = [r for r in rows] + list(stress_rows.values())
    for r in arm_dump:
        r.pop("_eq", None)
    meta = {
        "protocol": "qlib 参数扫描（模型不动、分数面板复用；预注册见脚本头）",
        "window": {"start": start, "end": end}, "signal": str(SIGNAL),
        "grid": {"topk": list(topks), "risk_degree": list(risks), "exit_rank": list(exits),
                 "entry_rank": ENTRY, "hold_rule": "band"},
        "costs": {"base": base, "stress": stress},
        "in_sample_warning": "同窗口同面板选参，存在过拟合；前沿只作研究候选，上线前走影子盘。",
        "front_labels": front,
        "arms": {r["label"]: r for r in arm_dump},
    }
    args.out.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2,
                                                   default=str), encoding="utf-8")
    pd.DataFrame(curves).to_parquet(OUT.parent / (OUT.name + "_curves.parquet"))
    print(f"产物：{args.out.with_suffix('.json')}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
