"""批次 P：标签周期 A/B（T+1→T+5 / T+10 / T+20）。

口径见 docs §20（写在读数之前）。模型侧由
`scripts/qlib_run.py --label-horizon H --fixed-trees 61 --pool-file ...` 训练并导出到
`data/qlib/model_batchP_labH/`；本脚本只做同一 band 纪律下的回测与读数：

- 三臂同一窗口、同一票池、同一成本、同一基准；交易纪律用 §10 A1 band 逐字相同
  （进 50 / 出 100 / 持 10），**不为长标签重调**——本批只隔离「标签」这一维。
- 主判据：P20−P5 配对日净差（÷√20）；记录项：P10−P5（÷√10）。
- 头部趋势诊断（前 10 名的 close≥MA20 比例、10/20 日收益中位数、分数与 20 日动量
  rank corr）是描述项，不参与裁决。
- 仪器门：P5 必须复现研究臂（分数面板逐点相同 + A1 四读数/净值曲线复现 §G4）；
  不过就整批读数作废。
"""
from __future__ import annotations

import argparse
import math
import os
import resource
import sys
import time
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

DATA = REPO / "data" / "qlib"
RESEARCH = (DATA / "model_research_trainend2021").resolve()
G_CURVES = DATA / "batch_G_curves.parquet"
CURVE_OUT = DATA / "batch_P_curves.parquet"

ARM_DIRS = {
    "P5": "model_batchP_lab5",
    "P10": "model_batchP_lab10",
    "P20": "model_batchP_lab20",
}
ARM_DESC = {
    "P5": "T+1→T+5（对照，复现研究臂）",
    "P10": "T+1→T+10（记录臂）",
    "P20": "T+1→T+20（主臂）",
}
HORIZON = {"P5": 5, "P10": 10, "P20": 20}
REF = "P5"
PRIMARY = "P20"
BAND = {"hold_rule": "band", "topk": 10, "entry_rank": 50, "exit_rank": 100}
MAIN_WINDOW = ("2023-01-03", "2026-10-15")
EXPECT_A1 = {"total_return": 0.2938, "annual_return": 0.0709,
             "max_drawdown": -0.2236, "one_way_turnover": 0.1428}
TOL_A1 = 0.001


def _head_trend(panel: pd.Series, pool: list[str], warm_beg: str, end: str) -> dict:
    """前 10 名的趋势状态（信号日 F 口径，描述项）。

    close≥MA20 比例、10 日收益中位数、20 日动量中位数：只看榜单头部是不是还在下跌；
    rank corr：整池分数与 20 日动量的秩相关（负 = 模型系统性偏向跌得多的票）。
    """
    import qlib.data

    frame = qlib.data.D.features(list(pool), ["$close"], start_time=warm_beg, end_time=end)["$close"]
    if list(frame.index.names[:2]) != ["datetime", "instrument"]:
        frame = frame.reorder_levels(["datetime", "instrument"])
    close = frame.sort_index().unstack("instrument").sort_index()
    ma20 = close.rolling(20).mean()
    ret10 = close / close.shift(10) - 1.0
    mom20 = close / close.shift(20) - 1.0

    rows = []
    for dt, grp in panel.groupby(level="datetime"):
        d = pd.Timestamp(dt)
        if d not in close.index:
            continue
        s = grp.droplevel("datetime").sort_values(ascending=False)
        top = list(s.index[:10])
        c = close.loc[d]
        above = [(c.get(ticker) >= ma20.loc[d].get(ticker)) for ticker in top
                 if pd.notna(c.get(ticker)) and pd.notna(ma20.loc[d].get(ticker))]
        r10 = [ret10.loc[d].get(ticker) for ticker in top if pd.notna(ret10.loc[d].get(ticker))]
        m20 = [mom20.loc[d].get(ticker) for ticker in top if pd.notna(mom20.loc[d].get(ticker))]
        common = pd.concat([s.rename("score"), mom20.loc[d].rename("mom")], axis=1, join="inner").dropna()
        corr = common["score"].corr(common["mom"], method="spearman") if len(common) >= 30 else float("nan")
        rows.append({"above20": sum(above) / len(above) if above else float("nan"),
                     "ret10": pd.Series(r10).median() if r10 else float("nan"),
                     "mom20": pd.Series(m20).median() if m20 else float("nan"),
                     "corr": corr})
    frame_out = pd.DataFrame(rows)
    return {k: float(frame_out[k].mean()) for k in frame_out.columns}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--start", default=MAIN_WINDOW[0])
    ap.add_argument("--end", default=MAIN_WINDOW[1])
    ap.add_argument("--capital", type=float, default=100_000_000.0)
    ap.add_argument("--benchmark", default="SH510300")
    ap.add_argument("--arms", default="P5,P10,P20", help="逗号分隔，如 P5,P20")
    args = ap.parse_args()

    # 先定 QLIB_MODEL_DIR 再 import：artifacts 在导入时读它；本批每臂再显式 load 自己的目录，
    # 这个默认值只是防止任何隐式路径落到线上 data/qlib/model。
    os.environ["QLIB_MODEL_DIR"] = str(RESEARCH)

    from stock_selector_qlib import artifacts, serving
    sys.path.insert(0, str(REPO / "scripts"))
    from qlib_hold_rule_ab import _benchmark_curve, _t, _yearly  # noqa: E402
    from stock_selector_qlib.backtest import _snap_window, run_qlib_backtest, score_panel  # noqa: E402

    names = [a.strip().upper() for a in args.arms.split(",") if a.strip()]
    unknown = [a for a in names if a not in ARM_DIRS]
    if unknown:
        raise SystemExit(f"不认识这些臂：{unknown}（可选 {list(ARM_DIRS)}）")
    missing = [n for n in names if not (DATA / ARM_DIRS[n]).is_dir()]
    if missing:
        raise SystemExit(f"缺模型产物目录：{[(n, ARM_DIRS[n]) for n in missing]}；"
                         "先用 scripts/qlib_run.py --label-horizon 训练并导出")

    serving.init_qlib()
    days = serving.calendar()
    beg, end = _snap_window(days, args.start, args.end)
    is_main = (beg, end) == MAIN_WINDOW
    warm_beg = days[max(0, days.index(beg) - 40)]
    print(f"窗口请求 {args.start}→{args.end}，实际撮合 {beg}→{end}"
          + ("（主窗口）" if is_main else "（非主窗口：只出读数，不判）"))

    bench_nav = _benchmark_curve(args.benchmark, beg, end, days)
    panels, results, curves = {}, {}, {}
    for name in names:
        art = artifacts.load(DATA / ARM_DIRS[name])
        print(f"\n[{name}] {ARM_DESC[name]}  产物 {ARM_DIRS[name]}  "
              f"label={art.meta.get('label')}  票池 {len(art.pool)} 只")
        t0 = time.time()
        panel = score_panel(art, beg, end)
        print(f"  打分面板 {len(panel)} 行 / {panel.index.get_level_values('datetime').nunique()} 天，"
              f"耗时 {time.time() - t0:.0f} 秒")
        panels[name] = panel
        t1 = time.time()
        result = run_qlib_backtest({"start": args.start, "end": args.end, "capital": args.capital,
                                    "engine": "qlib", "benchmark": args.benchmark, **BAND},
                                   signal=panel)
        results[name] = result
        curves[name] = pd.Series({pd.Timestamp(e["date"]): float(e["total_value"])
                                  for e in result.equity}).sort_index()
        print(f"  撮合完成，耗时 {time.time() - t1:.0f} 秒，"
              f"卖出 {result.metrics['sell_count']} 笔，成交 {result.metrics['trade_count']} 笔")

    # ---- 仪器门：P5 复现研究臂 ----
    gate_ok = True
    if is_main and REF in panels:
        print("\n=== 仪器门：P5 必须复现研究臂 ===")
        ref_art = artifacts.load(RESEARCH)
        ref_panel = score_panel(ref_art, beg, end)
        joined = pd.concat([panels[REF].rename("p5"), ref_panel.rename("ref")],
                           axis=1, join="inner").dropna()
        diff = float((joined["p5"] - joined["ref"]).abs().max()) if len(joined) else float("inf")
        ratio = len(joined) / max(len(ref_panel), 1)
        ok = diff <= 1e-9 and ratio >= 0.999
        gate_ok &= ok
        print(f"  {'过' if ok else '不过'}  分数面板 vs 研究臂：对齐 {len(joined)}/{len(ref_panel)} 行"
              f"（{ratio:.1%}），最大绝对差 {diff:.2e}（容差 1e-9）")

        print("\n=== 仪器门：P5 的 A1 band 复现 §G4 ===")
        m_a1 = dict(results[REF].metrics)
        m_a1["one_way_turnover"] = (m_a1["turnover_daily"] or 0.0) / 2.0
        for key, expected in EXPECT_A1.items():
            got = m_a1.get(key)
            delta = None if got is None else abs(got - expected)
            ok = delta is not None and delta <= TOL_A1
            gate_ok &= ok
            print(f"  {'过' if ok else '不过'}  {key}: 本次 {got} vs §G4 {expected}"
                  f"（差 {delta if delta is None else round(delta, 6)}，容差 {TOL_A1}）")
        if G_CURVES.exists():
            theirs = pd.read_parquet(G_CURVES)["A1"]
            cj = pd.concat([curves[REF].rename("o"), theirs.rename("g")], axis=1, join="inner").dropna()
            rel = float(((cj["o"] - cj["g"]).abs() / cj["g"].abs()).max()) if len(cj) else float("inf")
            ok = rel <= 1e-6 and len(cj) == len(theirs)
            gate_ok &= ok
            print(f"  {'过' if ok else '不过'}  净值曲线 vs batch_G_curves['A1']："
                  f"{len(cj)}/{len(theirs)} 天对齐，最大相对差 {rel:.2e}（容差 1e-6）")
        else:
            gate_ok = False
            print(f"  不过  {G_CURVES} 不存在，没法做曲线级复现")
    elif is_main:
        gate_ok = False
        print("\n  不过  主窗口没跑 P5，仪器门做不了（它是 §20 预注册的一部分）")

    if not gate_ok:
        print("\n读数作废：仪器门没过，先修仪器，不打印收益（docs §20.3）。")
        return 1

    # ---- 读数 ----
    print(f"\n=== 读数（扣成本净值；窗口 {beg}→{end}）===")
    for name in names:
        m = dict(results[name].metrics)
        m["one_way_turnover"] = (m["turnover_daily"] or 0.0) / 2.0
        print(f"\n[{name}] {ARM_DESC[name]}（标签 T+1→T+{HORIZON[name]}）")
        print(f"  总收益 {m['total_return']:.2%}（年化 {m['annual_return']:.2%}）  "
              f"基准 {m['benchmark_return']:.2%}  年化超额 {m['annual_excess']:.2%}")
        print(f"  最大回撤 {m['max_drawdown']:.2%}  夏普 {m['sharpe']}  "
              f"胜率 {m['win_rate']:.1%}  盈亏比 {m['payoff_ratio']}")
        print(f"  单边换手 {m['one_way_turnover']:.2%}/日  年成本 {m['cost_annualized']:.2%}  "
              f"平均持有 {m['avg_hold_days']} 天  成交 {m['trade_count']} 笔（卖出 {m['sell_count']}）")
        print(f"  期末 {m['end_value']:.0f}")
        yearly = _yearly(results[name].equity, bench_nav)
        print("  分年：" + "  ".join(f"{y}: {r:.1%}/超{b:+.1%}" for y, r, b in yearly))

    # ---- 头部趋势诊断（描述项）----
    if is_main:
        print("\n=== 头部趋势诊断（前 10 名，描述项，不参与裁决）===")
        for name in names:
            h = _head_trend(panels[name], artifacts.load(DATA / ARM_DIRS[name]).pool, warm_beg, end)
            print(f"  {name}: close≥MA20 {h['above20']:.1%} ｜ 10 日收益中位 {h['ret10']:+.2%}"
                  f" ｜ 20 日动量中位 {h['mom20']:+.2%} ｜ 分数与 20 日动量 rank corr {h['corr']:+.3f}")

    # ---- 配对与裁决 ----
    daily = {name: curves[name].pct_change().dropna() for name in names}
    pairs = [(name, REF) for name in names if name != REF and REF in daily]
    halves = [("2023-01-03", "2024-12-31"), ("2025-01-01", "2026-10-15")]

    print("\n=== 配对日净差（÷√H 按该臂标签周期折算；P5 是 5 日）===")
    for a, b in pairs:
        pair = (daily[a] - daily[b]).dropna()
        print(f"  {a}−{b}: 均值 {pair.mean() * 1e4:+.1f} bp/日  t={_t(pair):.2f}"
              f"（÷√{HORIZON[a]} → {_t(pair) / math.sqrt(HORIZON[a]):.2f}）  n={len(pair)}")

    print("\n=== 子窗一致性（预注册稳健性线）===")
    for a, b in pairs:
        pair = (daily[a] - daily[b]).dropna()
        for lo, hi in halves:
            half = pair.loc[lo:hi]
            print(f"  {a}−{b} {lo}→{hi}: {half.mean() * 1e4:+.1f} bp/日  t={_t(half):.2f}"
                  f"（÷√{HORIZON[a]} → {_t(half) / math.sqrt(HORIZON[a]):.2f}）  n={len(half)}")

    if is_main and PRIMARY in daily and REF in daily:
        pair = (daily[PRIMARY] - daily[REF]).dropna()
        t_adj = _t(pair) / math.sqrt(HORIZON[PRIMARY])
        halves_pair = [pair.loc[lo:hi].mean() for lo, hi in halves]
        ann_p = results[PRIMARY].metrics.get("annual_return")
        ann_ref = results[REF].metrics.get("annual_return")
        p10 = (daily["P10"] - daily[REF]).dropna() if "P10" in daily else None
        conds = {
            "a 点估计>0": pair.mean() > 0,
            f"b 整窗 t÷√{HORIZON[PRIMARY]}≥2": t_adj >= 2,
            "c 两子窗同正": all(v > 0 for v in halves_pair),
            "d 年化>P5": ann_p is not None and ann_ref is not None and ann_p > ann_ref,
            "e P10−P5 方向为正": p10 is not None and p10.mean() > 0,
        }
        passed = all(conds.values())
        verdict = ("进入独立样本复验（五条全过；仍不许直接上线）" if passed
                   else "不成立/未证明（硬线未全过）")
        print(f"\n=== 裁决（docs §20.4）：{PRIMARY} 主臂 = {verdict} ===")
        for key, ok in conds.items():
            print(f"  {'过' if ok else '不过'}  {key}")
        print(f"  {PRIMARY}−{REF} 点估计 {pair.mean() * 1e4:+.1f} bp/日（÷√{HORIZON[PRIMARY]} "
              f"t={t_adj:.2f}），子窗 {halves_pair[0] * 1e4:+.1f} / {halves_pair[1] * 1e4:+.1f} bp/日")

    out = pd.DataFrame(curves)
    out["bench"] = bench_nav
    out.to_parquet(CURVE_OUT)
    peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print(f"\n曲线落盘 {CURVE_OUT}（{out.shape[0]} 天 × {out.shape[1]} 列）  峰值内存 {peak_mb:.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
