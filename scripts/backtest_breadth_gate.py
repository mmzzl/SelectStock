"""买入腿的市场状态闸门：能不能靠「什么日子才买」把正期望做稳。

前三轮（出场参数、样本外剔除层、入场折价纪律）都测过并且结论一致：
**现有三条判据的买入腿日等权超额 ≈0 或在水下，所以任何只动卖出/只动下单的网格都不会
同时给出「赢比 2.0」和「赚钱」**（见 README《回测的「赢比」有两个口径》整章）。
这一轮动的是买入腿的**时机**：同一个信号，只在满足某个市场状态的日子出手。

状态量只用**当天收盘后就知道的数**（当天全样本等权涨幅、上涨家数占比），
因为信号本来就是收盘后算、次一交易日才下单，所以用当日状态不算前视；
但**绝对水位**（比如「今天大盘涨了 1%」）跨年不可比，所以两种口径都测：

- `--metric mean`：当日全样本等权涨幅（%），阈值是绝对百分点；
- `--metric advance`：当日上涨家数占比（%），50 是中性、阈值可正可负；
- `--metric heat`：个股所属板块近 3 日涨幅的**当日横截面百分位**（判定台账落盘），
  每天重新归一所以阈值跨时代可比，而且是个股级状态量（同一天里有的票过闸有的不过）。

方向两个都测而不是挑一个：「强势日顺势买」和「弱势日抄底买」在 A 股各有支持，
挑一个报数是研究者偏差不判据。

用法:
    python3 scripts/backtest_breadth_gate.py
    python3 scripts/backtest_breadth_gate.py --strategies pattern_reversal --universe 250
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 复用退出扫描那套同源机器（抽样、买入持有基线、跑一格回测），避免两处口径漂移
_spec = importlib.util.spec_from_file_location("backtest_exit_scan", ROOT / "scripts" / "backtest_exit_scan.py")
_scan = importlib.util.module_from_spec(_spec)
sys.modules["backtest_exit_scan"] = _scan
_spec.loader.exec_module(_scan)

DEFAULT_THRESHOLDS = "-0.5,0,0.5"

LEDGER_DIR = ROOT / "data" / "judgement_ledger"

_VALUES: dict[tuple, dict[str, float]] = {}
_HEAT: dict[tuple[int, int], tuple[dict, dict, np.ndarray]] = {}


class HeatGate:
    """个股「板块热度」闸门：热度 = 所属板块近 3 日等权涨幅的当日横截面百分位（0~100），
    由判定台账算好落盘（`data/judgement_ledger/ledger_YYYY.parquet`）。

    与 `BreadthGate` 的区别是这条路线值得单独测的原因：
    - 状态量是**横截面百分位**，每天重新归一，不随时代整体漂移，所以同一个阈值在
      2019/2022/2025 之间可比；全样本涨跌幅均值没这个性质（阈值换时代就得换数）。
    - 它是**个股级**状态量：同一天里有的票过闸有的不过，剔掉的是"冷板块里的信号"，
      不是"冷日子里的所有信号"。

    已知限度：成分股名单用的是台账落盘时的当前快照（`end_date` 取最大），历史上回溯贴标签
    必然带成分漂移；缺热度（无板块归属）的票按放行处理，与「信息不够就不判」一致。
    """

    def __init__(self, start: str, end: str, op: str, thr: float) -> None:
        self.op, self.thr = op, float(thr)
        self.dates, self.codes, self.matrix = _heat_matrix(int(start[:4]), int(end[:4]))
        self.days = len(self.dates)

    def __call__(self, date: str, code: str) -> bool:
        i = self.dates.get(str(date))
        j = self.codes.get(str(code))
        if i is None or j is None:
            return True
        v = self.matrix[i, j]
        if not np.isfinite(v):
            return True
        return bool(v >= self.thr) if self.op == "ge" else bool(v <= self.thr)


def _heat_matrix(y0: int, y1: int):
    """(日期→行号, 代码→列号, 热度矩阵)。矩阵 float32，250 交易日 × 5000 代码只有 5 MB，
    所以阈值切换不用重建表——一次装载，六个臂共用。"""
    key = (y0, y1)
    if key in _HEAT:
        return _HEAT[key]
    parts = []
    for y in range(y0, y1 + 1):
        f = LEDGER_DIR / f"ledger_{y}.parquet"
        if f.is_file():
            parts.append(pd.read_parquet(f, columns=["date", "code", "heat"]))
    if not parts:
        raise SystemExit(f"缺台账 {LEDGER_DIR}/ledger_{y0}.parquet，热度闸门无从取值")
    df = pd.concat(parts, ignore_index=True)
    date_list = sorted(df["date"].astype(str).unique())
    code_list = sorted(df["code"].astype(str).unique())
    di = {d: i for i, d in enumerate(date_list)}
    ci = {c: i for i, c in enumerate(code_list)}
    mat = np.full((len(date_list), len(code_list)), np.nan, dtype=np.float32)
    rows = df["date"].astype(str).map(di).to_numpy()
    cols = df["code"].astype(str).map(ci).to_numpy()
    mat[rows, cols] = df["heat"].to_numpy(dtype=np.float32)
    _HEAT[key] = (di, ci, mat)
    return _HEAT[key]


def _make_gate(metric: str, provider, universe: list[str], era: str, op: str, thr: float):
    start, end = era.split(":")
    if metric == "heat":
        return HeatGate(start, end, op, thr)
    return BreadthGate(provider, universe, start, end, metric, op, thr)


class BreadthGate:
    """当日市场状态闸门：与 RiskGate 同一个接口（date, code → 是否放行），所以能直接挂 KernelStrategy。

    状态表按 (时代, 抽样) 现算，代码只从当天全样本取 `pct`，不看任何未来行。
    当天算不出状态（停牌日/样本里没有行）按放行处理，与「信息不够就不判」一致。
    """

    def __init__(self, provider, universe: list[str], start: str, end: str,
                 metric: str, op: str, thr: float) -> None:
        self.op, self.thr, self.metric = op, float(thr), metric
        # 状态表只跟 (时代, 抽样, 状态量) 有关，与策略/出场无关：一套 9 张表给 63 格用，
        # 否则每格都重扫一遍全样本，跑不完。
        key = (start, end, metric, tuple(universe))
        if key not in _VALUES:
            _VALUES[key] = _build_values(provider, universe, start, end, metric)
        self.values = _VALUES[key]
        self.days = len(self.values)

    def __call__(self, date: str, code: str) -> bool:
        v = self.values.get(str(date))
        if v is None:
            return True
        return bool(v >= self.thr) if self.op == "ge" else bool(v <= self.thr)


def _build_values(provider, universe: list[str], start: str, end: str, metric: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for date in _scan_dates(provider, universe, start, end):
        rows = provider.rows(date, universe)
        pcts = [float(r.get("pct")) for r in rows.values() if r.get("pct") is not None]
        if len(pcts) < max(20, len(universe) // 4):
            continue
        arr = np.asarray(pcts, dtype=float)
        out[date] = float((arr > 0).mean() * 100) if metric == "advance" else float(arr.mean())
    return out


def _scan_dates(provider, universe: list[str], start: str, end: str) -> list[str]:
    """这个样本在这段时代里出现过的交易日（升序、去重）。

    用几只最长历史的票的日期集当骨架即可：日线是全市场对齐的交易日历，停牌只会少不会多，
    多出来的日期在 `provider.rows()` 那里自然取不到值。
    """
    dates: set[str] = set()
    for code in universe[:12]:
        w = provider.window(code, end)
        if w is None:
            continue
        w = w.loc[(w["date"] >= start) & (w["date"] <= end)]
        dates.update(str(d) for d in w["date"].tolist())
    return sorted(dates)


def run(args) -> None:
    provider = _scan.ParquetProvider(ROOT / "data" / "stock_kline")
    eras = args.eras.split(",")
    offsets = [int(x) for x in args.offsets.split(",")]
    exits = [tuple(float(x) for x in item.split(":")) for item in args.exits.split(",")]
    thresholds = [float(x) for x in args.thresholds.split(",")]
    metric = args.metric

    universes = {(era, offset): _scan._sample(provider.codes(), args.universe, offset)
                 for era in eras for offset in offsets}
    baselines = {key: _scan._buy_hold(provider, universes[key], key[0]) for key in universes}

    # 方向两个都跑：ge=只在状态高的时候买，le=只在状态低的时候买
    side = ("热板块", "冷板块") if metric == "heat" else ("强势日", "弱势日")
    arms: list[tuple[str, str, float]] = [("对照(无门)", "", 0.0)]
    for op, label in (("ge", side[0]), ("le", side[1])):
        for thr in thresholds:
            arms.append((f"{label}≥{thr:+.1f}" if op == "ge" else f"{label}≤{thr:+.1f}", op, thr))

    print(f"状态量={metric}（{args.thresholds}），时代 {len(eras)} × 抽样 {offsets}，"
          f"universe {args.universe}；闸门只看**当天收盘后**的状态量（热度取台账当日横截面百分位），"
          f"次一交易日下单口径不变", flush=True)

    results: list[dict] = []
    for name in args.strategies.split(","):
        for cfg in exits:
            for arm_label, op, thr in arms:
                cells = []
                for era in eras:
                    for offset in offsets:
                        gate = None if not op else _make_gate(
                            metric, provider, universes[(era, offset)], era, op, thr)
                        t0 = time.time()
                        r = _scan._run(provider, name, cfg, era, universes[(era, offset)], args, gate)
                        r["bh"] = baselines[(era, offset)]
                        r["gate_days"] = gate.days if gate is not None else len(_scan_dates(
                            provider, universes[(era, offset)], *era.split(":")))
                        cells.append((era[:4], offset, r))
                        print(f"  {name:17s} {arm_label:12s} 止损{cfg[0]:.2f} 止盈{cfg[1]:.2f} "
                              f"持有{cfg[2]:2.0f} 时代{era[:4]} 样本+{offset} n={r['n']:3d} "
                              f"剔{r['rej']:5d} 胜率{r['wr']:5.1%} "
                              f"payoff={_scan._num(r['payoff'], 99):5.2f} "
                              f"pf={_scan._num(r['pf'], 99):5.2f} "
                              f"期望={r['exp']:+7.1f} 超额={r['ret'] - r['bh']:+7.2%} "
                              f"回撤={r['dd']:+7.2%} ({time.time() - t0:.0f}s)", flush=True)
                payoffs = [_scan._num(c[2]["payoff"], 99.0) for c in cells]
                pfs = [_scan._num(c[2]["pf"], 99.0) for c in cells]
                wrs = [c[2]["wr"] for c in cells]
                exps = [c[2]["exp"] for c in cells]
                ns = [c[2]["n"] for c in cells]
                enough = min(ns) >= args.min_trades
                # 目标 ①+④：九格每一格都要 期望>0 且 赔率≥2.0 且 胜率≥50% 且 利润因子≥2.0。
                # 只看 payoff 会把"低胜率×高赔率"的截断结果当达标，所以四项一起判。
                payoff_ok = min(payoffs) >= args.payoff_target
                wr_ok = min(wrs) >= args.wr_target
                pf_ok = min(pfs) >= args.pf_target
                all_pos = bool(enough and min(exps) > 0)
                results.append({
                    "name": name, "cfg": cfg, "arm": arm_label, "cells": cells,
                    "pos": sum(1 for e in exps if e > 0), "total": len(exps),
                    "payoff_min": min(payoffs), "pf_min": min(pfs), "wr_min": min(wrs),
                    "exp_min": min(exps), "exp_mean": sum(exps) / len(exps),
                    "pass": bool(enough and payoff_ok and wr_ok and pf_ok and all_pos),
                    "all_pos": all_pos,
                    "n_min": min(ns),
                })

    print("\n===== 汇总：跨全部（时代 × 抽样）格 =====")
    for r in sorted(results, key=lambda x: (-x["pass"], -x["all_pos"], -x["pos"], -x["exp_mean"])):
        eras_bad = [f"{e}/{o}" for e, o, c in r["cells"] if c["exp"] <= 0]
        print(f"  {r['name']:17s} {r['arm']:12s} 止损{r['cfg'][0]:.2f} 止盈{r['cfg'][1]:.2f} "
              f"持有{r['cfg'][2]:2.0f} | 期望为正 {r['pos']}/{r['total']} "
              f"| payoff 最低 {r['payoff_min']:5.2f} | 胜率最低 {r['wr_min']:5.1%} "
              f"| 利润因子最低 {r['pf_min']:5.2f} | 期望 {r['exp_min']:+7.1f}~{r['exp_mean']:+7.1f}均值 "
              f"| 笔数最低 {r['n_min']:3d} | "
              + ("全格为正✓" if r["all_pos"] else "×")
              + (" 且 payoff≥2.0✓" if r["payoff_min"] >= args.payoff_target else "")
              + (f" 胜率≥{args.wr_target:.0%}✓" if r["wr_min"] >= args.wr_target else "")
              + (f" ①+④全格达标✓" if r["pass"] else "")
              + (f"  负格:{','.join(eras_bad)}" if eras_bad else ""))
    print("\n  「全格为正」= 3 时代 × 3 抽样的每一格单笔期望都>0（笔数下限 "
          f"{args.min_trades}）；这才是目标①。")
    print(f"  「①+④全格达标」= 每一格同时 期望>0 且 赔率≥{args.payoff_target:.1f} 且 "
          f"胜率≥{args.wr_target:.0%} 且 利润因子≥{args.pf_target:.1f}。")
    print("  只看 payoff 会把「低胜率×高赔率」的截断结果当达标，所以两项一起看。")
    print("  笔数最低那一栏必须看：闸门剔掉的日子越多，剩下的高越可能是「买得少」而不是「挑得准」。")
    print("  状态量是全样本等权口径，换一批股票（offset）它的分布也会变，所以阈值不能跨样本复用。")
    print("  只有跨 3 时代 × 3 抽样都同号的方向才值得写进实盘，单格翻正的一律当噪声。")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="买入腿市场状态闸门扫描")
    ap.add_argument("--strategies", default="pattern_reversal,box_oscillate")
    ap.add_argument("--exits", default="0.05:0.00:10,0.04:0.10:15")
    ap.add_argument("--eras", default=",".join(_scan.DEFAULT_ERAS))
    ap.add_argument("--offsets", default="0,137,274")
    ap.add_argument("--thresholds", default=DEFAULT_THRESHOLDS)
    ap.add_argument("--metric", choices=["mean", "advance", "heat"], default="mean")
    ap.add_argument("--universe", type=int, default=250)
    ap.add_argument("--capital", type=float, default=100000.0)
    ap.add_argument("--max-positions", type=int, default=5)
    ap.add_argument("--position-cap-pct", type=float, default=0.2)
    ap.add_argument("--target-amount", type=float, default=15000.0)
    ap.add_argument("--payoff-target", type=float, default=2.0)
    ap.add_argument("--wr-target", type=float, default=0.50, help="胜率目标（目标④的口径之一）")
    ap.add_argument("--pf-target", type=float, default=2.0, help="利润因子目标（目标④的口径之一）")
    ap.add_argument("--min-trades", type=int, default=30)
    ap.add_argument("--audit", action="store_true")
    run(ap.parse_args())
