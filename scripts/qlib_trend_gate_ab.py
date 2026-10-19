"""批次 O / O2：入场趋势闸门 A/B 实验（预注册见 docs §18、§19——先看线，再看数）。

同一份分数面板、同一套撮合，只差买入侧一道闸门：
  §18（批次 O）：A1 对照（无闸门，必须复现 §G4）/ G1 主臂（信号日收盘≥MA20）/
                  G2 次臂（收盘≥MA20 且 MA20 五日不向下）/ E1 探索（闸门[收盘≥MA20] + 全榜扫描补位）。
  §19（批次 O2）：H1 主臂（信号日 MA5>MA10）/ H2 探索（MA5>MA10 + 全榜扫描补位）。

用法（研究臂产物只读，不碰线上；本脚本会拒绝任何其它 QLIB_MODEL_DIR）：
    /usr/bin/python3 -u scripts/qlib_trend_gate_ab.py --start 2023-01-03 --end 2026-10-15 --arms A1,G1,G2,E1
    /usr/bin/python3 -u scripts/qlib_trend_gate_ab.py --start 2023-01-03 --end 2026-10-15 --arms A1,H1,H2
冒烟（只验跑得通，读数不判，仪器门 1a 不适用）：
    /usr/bin/python3 -u scripts/qlib_trend_gate_ab.py --start 2025-06-01 --end 2026-10-15 --arms A1,G1
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
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

RESEARCH_MODEL = (REPO / "data" / "qlib" / "model_research_trainend2021").resolve()

# 预注册的臂（docs §18.3 与 §19.2）：O 批主判据只看 G1，O2 批只看 H1（§18.4/§19.3 第 2 条）
ARMS = {
    "A1": {"label": "band 进50/出100/持10（对照=§10 A1）", "gate": None, "scan_all": False},
    "G1": {"label": "A1 + 闸门[收盘≥MA20]（批次 O 主臂）", "gate": "ma20", "scan_all": False},
    "G2": {"label": "A1 + 闸门[收盘≥MA20 且 MA20 五日不向下]", "gate": "ma20_slope",
           "scan_all": False},
    "E1": {"label": "闸门[收盘≥MA20] + 全榜扫描补位（探索）", "gate": "ma20", "scan_all": True},
    "H1": {"label": "A1 + 闸门[MA5>MA10]（批次 O2 主臂）", "gate": "ma5_ma10", "scan_all": False},
    "H2": {"label": "闸门[MA5>MA10] + 全榜扫描补位（探索）", "gate": "ma5_ma10", "scan_all": True},
}
BAND = {"hold_rule": "band", "topk": 10, "entry_rank": 50, "exit_rank": 100}

# 仪器门 1a（docs §18.4）：§G4 A1 的四个读数与曲线文件
MAIN_WINDOW = ("2023-01-03", "2026-10-15")
EXPECT_A1 = {"total_return": 0.2938, "annual_return": 0.0709,
             "max_drawdown": -0.2236, "one_way_turnover": 0.1428}
TOL_A1 = 0.001  # 0.1pp
G_CURVES = REPO / "data" / "qlib" / "batch_G_curves.parquet"
CURVE_DIR = REPO / "data" / "qlib"


def build_gate_panel(pool, warm_beg: str, end: str) -> dict[str, pd.Series]:
    """两张闸门面板（0/1，index=(datetime, instrument)）。

    只用 ≤F 日的收盘价滚动出 MA20（as-of，与模型分数同一信息集）；比较产生 NaN 的位置
    在 pandas 里本来就是 False，正好对应「判不了就关门」。面板多出来的暖机段无害，
    策略只按信号日 F 取数。
    """
    import qlib.data

    frame = qlib.data.D.features(list(pool), ["$close"], start_time=warm_beg, end_time=end)["$close"]
    if list(frame.index.names[:2]) != ["datetime", "instrument"]:
        frame = frame.reorder_levels(["datetime", "instrument"])
    frame = frame.sort_index()
    close = frame.unstack("instrument").sort_index()
    ma20 = close.rolling(20).mean()
    masks = {
        "ma20": close >= ma20,
        "ma20_slope": (close >= ma20) & (ma20 >= ma20.shift(5)),
        "ma5_ma10": close.rolling(5).mean() > close.rolling(10).mean(),
    }
    gates: dict[str, pd.Series] = {}
    for name, mask in masks.items():
        long = mask.astype(float).stack(future_stack=True)
        gates[name] = long.rename(name)
    return gates


def _coverage(gates: dict[str, pd.Series], panel: pd.Series) -> dict[str, pd.DataFrame]:
    """逐日：前 50 名里通过几个、前 10 名里通过几个、全池通过率。"""
    out: dict[str, pd.DataFrame] = {}
    for name, gate in gates.items():
        df = pd.DataFrame({"score": panel, "gate": gate}).dropna(subset=["score"])
        rows = []
        for _, grp in df.groupby(level="datetime"):
            if len(grp) < 100:
                continue
            ranked = grp.sort_values("score", ascending=False)["gate"]
            rows.append((ranked.head(50).mean(), ranked.head(10).mean(), ranked.mean()))
        out[name] = pd.DataFrame(rows, columns=["top50", "top10", "pool"])
    return out


def _check_gate_on_buys(result, gate: pd.Series, prev_day: dict[str, str],
                        code2sym: dict[str, str]) -> tuple[int, list[tuple]]:
    """仪器门 1b：每一笔买入在信号日 F（= 成交日的前一交易日）必须通过闸门。"""
    lut = {(str(d)[:10], sym): float(v) for (d, sym), v in gate.items()}
    n, bad = 0, []
    for trade in result.trades:
        if trade["side"] != "buy":
            continue
        n += 1
        f = prev_day.get(trade["date"])
        sym = code2sym.get(trade["code"])
        value = lut.get((f, sym)) if f is not None and sym is not None else None
        if value is None or value < 0.5:
            bad.append((trade["date"], trade["code"], f, value))
    return n, bad


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--start", default=MAIN_WINDOW[0])
    ap.add_argument("--end", default=MAIN_WINDOW[1])
    ap.add_argument("--capital", type=float, default=100_000_000.0)
    ap.add_argument("--benchmark", default="SH510300")
    ap.add_argument("--arms", default="A1,G1,G2,E1", help="逗号分隔，如 A1,G1")
    args = ap.parse_args()

    env_dir = os.environ.get("QLIB_MODEL_DIR") or str(RESEARCH_MODEL)
    if Path(env_dir).resolve() != RESEARCH_MODEL:
        raise SystemExit(f"批次 O 只读研究臂产物 {RESEARCH_MODEL}（收到 QLIB_MODEL_DIR={env_dir!r}）；"
                         f"线上产物 data/qlib/model 不在本批范围内")
    os.environ["QLIB_MODEL_DIR"] = str(RESEARCH_MODEL)

    names = [a.strip().upper() for a in args.arms.split(",") if a.strip()]
    unknown = [a for a in names if a not in ARMS]
    if unknown:
        raise SystemExit(f"不认识这些臂：{unknown}（可选 {list(ARMS)}）")

    # 必须先定 QLIB_MODEL_DIR 再 import：artifacts 在模块导入时读这个环境变量
    from stock_selector_qlib import artifacts, serving
    sys.path.insert(0, str(REPO / "scripts"))
    from qlib_hold_rule_ab import _benchmark_curve, _t, _yearly  # noqa: E402
    from stock_selector_qlib.backtest import _snap_window, run_qlib_backtest, score_panel  # noqa: E402

    art = artifacts.load()
    print(f"模型产物：{art.describe()}  票池 {len(art.pool)} 只")
    serving.init_qlib()
    days = serving.calendar()
    beg, end = _snap_window(days, args.start, args.end)
    is_main = (beg, end) == MAIN_WINDOW
    warm_beg = days[max(0, days.index(beg) - 40)]
    print(f"窗口请求 {args.start}→{args.end}，实际撮合 {beg}→{end}"
          + ("（主窗口）" if is_main else "（冒烟窗口：读数不判）"))

    t0 = time.time()
    panel = score_panel(art, beg, end)
    print(f"打分面板 {len(panel)} 行 / {panel.index.get_level_values('datetime').nunique()} 天，"
          f"耗时 {time.time() - t0:.0f} 秒")

    gates = build_gate_panel(art.pool, warm_beg, end)

    print("\n=== 闸门覆盖率（信号日 F 的榜单口径）===")
    cov = _coverage(gates, panel)
    for name, arr in cov.items():
        print(f"  {name:<10} 全池通过率 {arr['pool'].mean():.1%} ｜ 前50 通过 {arr['top50'].mean() * 50:.1f}/50"
              f" ｜ 前10 通过 {arr['top10'].mean() * 10:.1f}/10 ｜ {len(arr)} 天")

    bench_nav = _benchmark_curve(args.benchmark, beg, end, days)
    results, curves = {}, {}
    for name in names:
        spec = ARMS[name]
        params = {"start": args.start, "end": args.end, "capital": args.capital,
                  "engine": "qlib", "benchmark": args.benchmark, **BAND}
        if spec["gate"]:
            params["entry_gate"] = gates[spec["gate"]]
            params["scan_all"] = spec["scan_all"]
        t1 = time.time()
        result = run_qlib_backtest(params, signal=panel)
        results[name] = result
        curves[name] = pd.Series({pd.Timestamp(e["date"]): float(e["total_value"])
                                  for e in result.equity}).sort_index()
        print(f"  {name} 撮合完成，耗时 {time.time() - t1:.0f} 秒，"
              f"卖出 {result.metrics['sell_count']} 笔，成交 {result.metrics['trade_count']} 笔")

    # ---- 仪器门 1b：闸门落实自检 ----
    print("\n=== 仪器门 1b：每笔买入的信号日必须通过该臂闸门 ===")
    prev_day = {days[i]: days[i - 1] for i in range(1, len(days))}
    code2sym = {s[2:]: s for s in art.pool}
    gate1b_ok = True
    for name in names:
        spec = ARMS[name]
        if not spec["gate"]:
            continue
        n, bad = _check_gate_on_buys(results[name], gates[spec["gate"]], prev_day, code2sym)
        ok = n > 0 and not bad
        gate1b_ok &= ok
        print(f"  {'过' if ok else '不过'}  {name}：买入 {n} 笔，信号日未过闸门 {len(bad)} 笔"
              + (f"（例：{bad[:3]}）" if bad else ""))

    # ---- 仪器门 1a：A1 复现 §G4（只在主窗口判）----
    gate1a_ok = True
    if is_main and "A1" in names:
        print("\n=== 仪器门 1a：A1 复现 §G4 ===")
        m_a1 = dict(results["A1"].metrics)
        m_a1["one_way_turnover"] = (m_a1["turnover_daily"] or 0.0) / 2.0
        for key, expected in EXPECT_A1.items():
            got = m_a1.get(key)
            delta = None if got is None else abs(got - expected)
            ok = delta is not None and delta <= TOL_A1
            gate1a_ok &= ok
            print(f"  {'过' if ok else '不过'}  {key}: 本次 {got} vs §G4 {expected}"
                  f"（差 {delta if delta is None else round(delta, 6)}，容差 {TOL_A1}）")
        if G_CURVES.exists():
            theirs = pd.read_parquet(G_CURVES)["A1"]
            joined = pd.concat([curves["A1"].rename("o"), theirs.rename("g")], axis=1, join="inner").dropna()
            rel = float(((joined["o"] - joined["g"]).abs() / joined["g"].abs()).max())
            ok = rel <= 1e-6 and len(joined) == len(theirs)
            gate1a_ok &= ok
            print(f"  {'过' if ok else '不过'}  净值曲线 vs batch_G_curves['A1']："
                  f"{len(joined)}/{len(theirs)} 天对齐，最大相对差 {rel:.2e}（容差 1e-6）")
        else:
            gate1a_ok = False
            print(f"  不过  {G_CURVES} 不存在，没法做曲线级复现")
    elif is_main:
        gate1a_ok = False
        print("\n  不过  主窗口没跑 A1，没法做 §G4 复现（仪器门 1a 是预注册的一部分）")
    else:
        print("\n（冒烟窗口：仪器门 1a 不适用，§18 只认主窗口）")

    if not (gate1b_ok and gate1a_ok):
        print("\n读数作废：仪器门没过，先修仪器，不打印收益（docs §18.4 第 1 条）。")
        return 1

    # ---- 读数 ----
    print(f"\n=== 读数（扣成本净值；窗口 {beg}→{end}）===")
    for name in names:
        m = dict(results[name].metrics)
        m["one_way_turnover"] = (m["turnover_daily"] or 0.0) / 2.0
        print(f"\n[{name}] {ARMS[name]['label']}")
        print(f"  总收益 {m['total_return']:.2%}（年化 {m['annual_return']:.2%}）  "
              f"基准 {m['benchmark_return']:.2%}  年化超额 {m['annual_excess']:.2%}")
        print(f"  最大回撤 {m['max_drawdown']:.2%}  夏普 {m['sharpe']}  "
              f"胜率 {m['win_rate']:.1%}  盈亏比 {m['payoff_ratio']}")
        print(f"  单边换手 {m['one_way_turnover']:.2%}/日  年成本 {m['cost_annualized']:.2%}  "
              f"平均持有 {m['avg_hold_days']} 天  成交 {m['trade_count']} 笔（卖出 {m['sell_count']}）")
        print(f"  期末 {m['end_value']:.0f}  毛利 {m['gross_return']:.2%}（扣成本前，别单看）")
        yearly = _yearly(results[name].equity, bench_nav)
        print("  分年：" + "  ".join(f"{y}: {r:.1%}/超{b:+.1%}" for y, r, b in yearly))

    # ---- 显著性 ----
    print("\n=== 显著性（t 未折算；÷√5 是重叠标签的折算口径，预注册主判据用折算值）===")
    daily = {name: curves[name].pct_change().dropna() for name in names}
    bench_daily = bench_nav.pct_change().dropna()
    for name in names:
        d = daily[name]
        excess = (d - bench_daily).dropna()
        print(f"  {name}: 日净 t={_t(d):.2f}（÷√5 → {_t(d) / math.sqrt(5):.2f}）｜"
              f" vs 基准超额均值 {excess.mean() * 1e4:+.1f} bp/日 t={_t(excess):.2f}"
              f"（÷√5 → {_t(excess) / math.sqrt(5):.2f}）")
    pairs = [(name, "A1") for name in names if name != "A1" and "A1" in daily]
    for a, b in pairs:
        pair = (daily[a] - daily[b]).dropna()
        print(f"  {a}−{b} 配对日净差：均值 {pair.mean() * 1e4:+.1f} bp/日  t={_t(pair):.2f}"
              f"（÷√5 → {_t(pair) / math.sqrt(5):.2f}）  n={len(pair)}")

    print("\n=== 子窗一致性（预注册稳健性线）===")
    halves = [("2023-01-03", "2024-12-31"), ("2025-01-01", "2026-10-15")]
    for a, b in pairs:
        pair = (daily[a] - daily[b]).dropna()
        for lo, hi in halves:
            half = pair.loc[lo:hi]
            print(f"  {a}−{b} {lo}→{hi}: {half.mean() * 1e4:+.1f} bp/日  t={_t(half):.2f}"
                  f"（÷√5 → {_t(half) / math.sqrt(5):.2f}）  n={len(half)}")

    # ---- 主判据裁决（只在主窗口；主臂按批次：O=G1，O2=H1）----
    primary = "G1" if "G1" in daily else ("H1" if "H1" in daily else None)
    if is_main and primary and "A1" in daily:
        pair = (daily[primary] - daily["A1"]).dropna()
        t5 = _t(pair) / math.sqrt(5)
        halves_pair = [pair.loc[lo:hi].mean() for lo, hi in halves]
        both_positive = all(v > 0 for v in halves_pair)
        if primary == "H1":
            ann_h1 = results["H1"].metrics.get("annual_return")
            ann_a1 = results["A1"].metrics.get("annual_return")
            annual_better = (ann_h1 is not None and ann_a1 is not None and ann_h1 > ann_a1)
            passed = pair.mean() > 0 and t5 >= 2 and both_positive and annual_better
            verdict = ("进入独立样本复验（四条全过；仍不许直接上线）" if passed
                       else "不成立（四条硬线未全过）")
            rule = "docs §19.3 第 2 条"
        else:
            if pair.mean() < 0 and t5 < -2:
                verdict = "有害"
            elif pair.mean() > 0 and t5 > 2:
                verdict = "过" if both_positive else "不稳定（整窗显著为正但子窗方向不一致）"
            else:
                verdict = "未证明"
            rule = "docs §18.4 第 2/3 条"
        print(f"\n=== 裁决（{rule}）：{primary} 主臂 = {verdict} ===")
        print(f"  {primary}−A1 点估计 {pair.mean() * 1e4:+.1f} bp/日（÷√5 t={t5:.2f}），"
              f"子窗 {halves_pair[0] * 1e4:+.1f} / {halves_pair[1] * 1e4:+.1f} bp/日")

    tags = []
    if any(n in ("G1", "G2", "E1") for n in names):
        tags.append("O")
    if any(n in ("H1", "H2") for n in names):
        tags.append("O2")
    curve_out = CURVE_DIR / f"batch_{''.join(tags) or 'O'}_curves.parquet"
    out = pd.DataFrame(curves)
    out["bench"] = bench_nav
    out.to_parquet(curve_out)
    peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print(f"\n曲线落盘 {curve_out}（{out.shape[0]} 天 × {out.shape[1]} 列）  峰值内存 {peak_mb:.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
