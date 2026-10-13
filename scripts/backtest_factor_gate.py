"""因子闸门回测：把筛选出的「特征分位条件」挂到买入腿，测它能不能把 ①④ 一起顶上去。

为什么筛选之后还要再跑一遍回测：筛选那一层量的是「这一箱 vs 同格全部命中」的**非配对**
对比，两边命中的日子不同、市场水位不同，噪声很大（本项目 20 个够格的候选里合并
t 最高只有 0.27）。而闸门臂与对照臂在**同一格、同一批股票、同一个引擎**里跑，
唯一差别是"这一天这批命中里买不买某一部分"——这是配对对比，灵敏度高一个量级。
所以筛选的职责只是**生成候选**，判定归回测。

达标口径（目标 ①+④，写死在 `--wr-target` / `--payoff-target` / `--pf-target`）：
3 时代 × 3 抽样 = 9 格，每一格同时满足
    单笔期望 > 0  且  赔率 ≥ 2.0  且  胜率 ≥ 50%  且  利润因子 ≥ 2.0  且  笔数 ≥ 下限
只过一个都不算过：赔率可以靠截断亏损凑出来而期望仍为负，胜率可以靠"多买多亏"压到
50% 以下换高赔率，利润因子可以在胜率很低时照样过 2。

闸门取值口径与筛选完全一致：特征在**当日样本内**的横截面百分位。
分位依赖样本这一点在报告里要跟着一起读——换 offset 就换了一批股票，
所以九格同号是这里唯一的硬证据；单格好看的一律当噪声。

用法:
    python3 scripts/backtest_factor_gate.py                       # 自动取筛选表里够格的候选
    python3 scripts/backtest_factor_gate.py --rules adx14:4,pct:4  # 手工指定 特征:箱号
    python3 scripts/backtest_factor_gate.py --rules adx14:4 --exits 0.04:0.10:15
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

_spec = importlib.util.spec_from_file_location("backtest_exit_scan", ROOT / "scripts" / "backtest_exit_scan.py")
_scan = importlib.util.module_from_spec(_spec)
sys.modules["backtest_exit_scan"] = _scan
_spec.loader.exec_module(_scan)

# 因子定义直接复用筛选脚本：闸门与筛选若各写一份派生公式（liq = volume×close/1e8
# 这种），迟早会漂成两个东西，而漂了的那个没人看得出来——所以这里不重抄。
_ssf_spec = importlib.util.spec_from_file_location("screen_signal_factors",
                                                   ROOT / "scripts" / "screen_signal_factors.py")
_ssf = importlib.util.module_from_spec(_ssf_spec)
sys.modules["screen_signal_factors"] = _ssf
_ssf_spec.loader.exec_module(_ssf)
DERIVED_COLS = list(_ssf.DERIVED_COLS)
DERIVED_INPUTS = ["close", "volume", "atr14", "ma5", "ma20"]   # `_add_derived` 用到的原始列

from stock_selector.backtest.provider import ParquetProvider            # noqa: E402

KLINE_DIR = ROOT / "data" / "stock_kline"
SCREEN_DIR = ROOT / "data" / "factor_screen"
BINS = 5

_RANKS: dict[tuple, dict[str, dict]] = {}


def _load_ranks(start: str, end: str, universe: tuple[str, ...], factors: list[str]) -> dict:
    """(日期, 代码) → 当日横截面百分位（0~100）。一次装载，六个臂共用。

    只读 `date` 与所需特征两列（`read_parquet(columns=…)`），不把 82 列日线塞进内存：
    9 个 (时代, 抽样) × 250 只票的整表缓存会把进程撑到 GB 级，而闸门只在命中时才查。
    """
    key = (start, end, universe, tuple(factors))
    if key in _RANKS:
        return _RANKS[key]
    native = [f for f in factors if f not in DERIVED_COLS]
    need = sorted({*native, *(DERIVED_INPUTS if len(native) < len(factors) else [])})
    frames = []
    for code in universe:
        path = KLINE_DIR / f"{code}.parquet"
        if not path.exists():
            continue
        try:
            df = pd.read_parquet(path, columns=["date", *need])
        except Exception as exc:
            # 列不存在必须**立刻**炸出来：`liq`/`atr_pct` 这些是派生列，parquet 里
            # 并没有；若在这里 continue，每只票都会静默被跳过，最后表现为
            # 「这批股票没有日线」——一个与真实原因完全无关的报错。
            raise SystemExit(f"闸门取数失败 {path.name}：{exc}；可用特征见 "
                             f"{','.join(_ssf.FACTORS)}（派生列 {','.join(DERIVED_COLS)} "
                             f"由 {','.join(DERIVED_INPUTS)} 现算）") from exc
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        df = df[(df["date"] >= start) & (df["date"] <= end)]
        if df.empty:
            continue
        df["code"] = code
        frames.append(df)
    if not frames:
        raise SystemExit(f"闸门取数失败：{start}~{end} 这批股票在日线目录里没有行")
    panel = pd.concat(frames, ignore_index=True)
    if len(native) < len(factors):
        panel = pd.concat([panel.reset_index(drop=True),
                           _ssf._add_derived(panel.reset_index(drop=True))], axis=1)
    out: dict[str, dict] = {}
    for f in factors:
        vals = pd.to_numeric(panel[f], errors="coerce")
        rank = vals.groupby(panel["date"]).rank(pct=True, method="average") * 100.0
        out[f] = dict(zip(zip(panel["date"], panel["code"]), rank))
    _RANKS[key] = out
    return out


class FactorGate:
    """个股级「特征分位闸门」：命中之后、下单之前问一次 (date, code) → 放不放行。

    与 `HeatGate` 同一个接口，所以能直接挂 `KernelStrategy.reject_gate`，不需要改引擎。

    取不到分位（停牌、指标未成形、这批票当天没行）按**放行**处理——与实盘
    「信息不够就不判」一致。若这里改成「取不到就剔」，闸门会在数据稀疏的票上
    悄悄砍掉一大截，回测笔数掉得比想象中多，而原因不在判据。
    """

    def __init__(self, start: str, end: str, universe: tuple[str, ...],
                 rules: list[dict], mode: str = "union") -> None:
        self.rules = rules
        self.mode = mode
        factors = sorted({str(r["factor"]) for r in rules})
        self.ranks = _load_ranks(start, end, universe, factors)

    def __call__(self, date: str, code: str) -> bool:
        step = 100.0 / BINS
        hits = decided = 0
        for rule in self.rules:
            table = self.ranks.get(str(rule["factor"]))
            if not table:
                continue
            v = table.get((str(date), str(code)))
            if v is None or not np.isfinite(v):
                continue                       # 信息不够 → 这一条不判
            decided += 1
            if "min_pct" in rule:
                # `>=百分位` 写法：`>=90` 与分位正好等于 90 的票同侧，闭区间不设边界坑
                if v >= float(rule["min_pct"]) - 1e-9:
                    hits += 1
            # 箱写法：最高那一档要含 100 分位本身，写 `lo <= v < hi` 会把「当日最高」
            # 那只票判成不在 [80,100) 里，而候选规则几乎全是 bin4——等于接到空集上。
            elif min(int(v // step), BINS - 1) == int(rule["bin"]):
                hits += 1
        if decided == 0:
            return True                       # 一条都判不了 → 放行（与 HeatGate 同口径）
        need = len(self.rules) if self.mode == "all" else 1
        return hits >= need


def parse_rules(text: str) -> list[dict]:
    """`--rules` 的格式校验，两种写法：

    - `特征:箱号`（0~4，五分位箱，与筛选表同一口径）；
    - `特征:>=百分位`（如 `adx14:>=90`，比箱更细，用于测更严的闸门）。

    格式错必须**立刻**报错：默默按"全放行"处理会让这一臂退化成对照臂，
    而报告里它还挂着一个规则的名字，读数会被当成"这条规则没用"。
    """
    rules = []
    for item in str(text or "").split(","):
        item = item.strip()
        if not item:
            continue
        factor, _, box = item.partition(":")
        rule: dict = {"factor": factor, "why": "手工指定"}
        if box.startswith(">="):
            pct = box[2:].strip()
            if not pct.replace(".", "").isdigit() or not 0 < float(pct) <= 100:
                raise SystemExit(f"--rules 百分位需在 (0,100]，收到 {item!r}")
            rule["min_pct"] = float(pct)
        elif box.isdigit() and 0 <= int(box) < BINS:
            rule["bin"] = int(box)
        else:
            raise SystemExit(f"--rules 格式应为 特征:{0}-{BINS - 1} 或 特征:>=百分位，收到 {item!r}")
        rules.append(rule)
    if not rules:
        raise SystemExit("--rules 为空；留空表示自动取筛选表候选")
    return rules


def load_candidates(strategy: str, args) -> list[dict]:
    """默认候选 = 筛选表里「九格都有样本」的，按 |合并 d期望| 降序取前 N。

    这里**不**按 t 或同号筛：筛选层的 t 在日级口径下天然很小（命中一天只有几条，
    日均值的方差被少数极端日支配），拿它当闸门候选的门槛会一个都不剩。
    真正的判据在回测这一层——九格配对对比。
    """
    path = SCREEN_DIR / f"screen_exit_{strategy}.parquet"
    if not path.is_file():
        raise SystemExit(f"缺 {path}，先跑 screen_signal_factors.py")
    df = pd.read_parquet(path)
    df = df[(df["cells"] >= 9) & (df["d_exp_pp"] > 0)]
    if df.empty:
        raise SystemExit("筛选表里没有九格样本足够且 d期望为正的候选")
    df = df.sort_values("d_exp_pp", ascending=False).head(args.max_candidates)
    seen, rules = set(), []
    for _, r in df.iterrows():
        key = (str(r["factor"]), int(r["bin"]))
        if key in seen:
            continue
        seen.add(key)
        rules.append({"factor": key[0], "bin": key[1],
                      "why": f"筛选 d期望{r['d_exp_pp']:+.2f}pp 九格{('同号' if r['sign_stable'] else '非同号')}"
                             f" 覆盖{r['coverage']:.0%} @{r['cfg']}"})
    return rules


def render(results: list[dict], args) -> str:
    """两段：一段看绝对达标（①+④ 的字面口径），一段看相对改进（配对差值九格同号）。

    两段必须分开：绝对达标是"能不能上线"，相对改进是"闸门有没有用"。
    一个臂完全可能九格都变好看但仍然达不到 50% 胜率——那说明闸门方向对、
    但买入腿本身还不够，这种话不能混成一句"有效果"。
    """
    out = ["", "===== 绝对达标：九格每格同时满足 期望>0 且 赔率≥%.1f 且 胜率≥%.0f%% 且 利润因子≥%.1f 且 笔数≥%d ====="
           % (args.payoff_target, args.wr_target * 100, args.pf_target, args.min_trades)]
    for r in results:
        marks = "全格达标✓" if r["pass"] else "×"
        out.append(f"  {r['arm']:26s} {marks:10s} 达标格 {r['pass_cells']}/{len(r['cells'])} "
                   f"| 胜率 {r['wr_min']:.1%}~{r['wr_max']:.1%} | 赔率 {r['payoff_min']:.2f}~{r['payoff_max']:.2f} "
                   f"| 利润因子 {r['pf_min']:.2f}~{r['pf_max']:.2f} | 期望 {r['exp_min']:+7.1f}~{r['exp_max']:+7.1f} "
                   f"| 笔数 {r['n_min']}~{r['n_max']}")
    out += ["", "===== 相对对照的配对差值（同一格、同一批股票，只差闸门） ====="]
    for r in results:
        if r["arm"] == "对照(无门)":
            continue
        cells = r["cells"]
        out.append(f"  {r['arm']:26s} d胜率 {r['d_wr'][0]:+.1%}~{r['d_wr'][1]:+.1%} 均{r['d_wr_mean']:+.1%} "
                   f"九格同号{'✓' if r['wr_stable'] else '×'} | d赔率 {r['d_payoff'][0]:+.2f}~{r['d_payoff'][1]:+.2f} "
                   f"九格同号{'✓' if r['payoff_stable'] else '×'} | d利润因子 {r['d_pf'][0]:+.2f}~{r['d_pf'][1]:+.2f} "
                   f"| d期望 {r['d_exp'][0]:+.1f}~{r['d_exp'][1]:+.1f} 均{r['d_exp_mean']:+.1f} "
                   f"同号{'✓' if r['exp_stable'] else '×'} | 笔数 {r['n_min']}~{r['n_max']}（对照 {r['ctl_n_min']}~{r['ctl_n_max']}）")
    out += ["",
            "  读法：d 那一组是**同一格内**闸门臂减对照臂，所以市场水位、时代涨跌都被抵消了；",
            "  但剔掉的票越多，剩下的高越可能是「买得少」而不是「挑得准」——所以笔数与仓位必须一起看，",
            "  判「挑得准不准」只看胜率与赔率，不看总收益（总收益里混着仓位差）。",
            f"  九格同号只在这一族里可信：分位是当日样本内横截面，换 offset 就换了股票池。",
            "  一个臂若只在 2019 那一行好看、2025 两行翻负，那是时代，不是判据。"]
    return "\n".join(out)


def run(args) -> None:
    provider = ParquetProvider(KLINE_DIR)
    eras = args.eras.split(",")
    offsets = [int(x) for x in args.offsets.split(",")]
    exits = [tuple(float(x) for x in item.split(":")) for item in args.exits.split(",")]
    name = args.strategies
    rules = parse_rules(args.rules) if args.rules else load_candidates(name, args)

    universes = {(era, off): _scan._sample(provider.codes(), args.universe, off)
                 for era in eras for off in offsets}
    baselines = {key: _scan._buy_hold(provider, universes[key], key[0]) for key in universes}
    def arm_label(rule: dict) -> str:
        if "min_pct" in rule:
            return f"{rule['factor']}>={rule['min_pct']:.0f}"
        return f"{rule['factor']}[{rule['bin'] * 100 // BINS},{(rule['bin'] + 1) * 100 // BINS})"

    arms: list[tuple[str, list[dict] | None]] = [("对照(无门)", None)]
    arms += [(arm_label(r), [r] if args.mode == "single" else rules) for r in rules]

    print(f"策略 {name}；闸门 = 当日横截面分位；时代 {len(eras)} × 抽样 {offsets}，universe {args.universe}")
    print(f"候选 {len(rules)} 条，mode={args.mode}（single=每条规则单独一臂，"
          f"union=全部规则取并集，all=要求全部满足）", flush=True)
    for r in rules:
        print(f"    {arm_label(r)} {r['why']}")

    results: list[dict] = []
    for cfg in exits:
        print(f"\n----- 出场参数 止损{cfg[0]:.2f} 止盈{cfg[1]:.2f} 持有{cfg[2]:.0f} -----")
        control_cells: list[tuple] = []
        for arm_label, gate_rules in arms:
            cells = []
            # offset 套在 era 外层：股票池只跟 offset 有关，同一个 offset 的三个时代
            # 共用一批代码，缓存才有意义。反过来（era 外层）每个 cell 换一次池，
            # 800 只票每格重读一遍 parquet，时间全花在 I/O 上。
            for off in offsets:
                for era in eras:
                    t0 = time.time()
                    gate = (FactorGate(*era.split(":"), tuple(universes[(era, off)]), gate_rules,
                                       args.mode) if gate_rules else None)
                    r = _scan._run(provider, name, cfg, era, universes[(era, off)], args, gate)
                    r["bh"] = baselines[(era, off)]
                    cells.append((era[:4], off, r))
                    print(f"  {arm_label:24s} 止损{cfg[0]:.2f} 止盈{cfg[1]:.2f} 持有{cfg[2]:2.0f} "
                          f"时代{era[:4]} 样本+{off:3d} n={r['n']:3d} 剔{r['rej']:4d} "
                          f"胜率{r['wr']:5.1%} 赔率={_scan._num(r['payoff'], 99):5.2f} "
                          f"利润因子={_scan._num(r['pf'], 99):6.2f} 期望={r['exp']:+7.1f} "
                          f"仓={r['expo']:.0%} 超额={r['ret'] - r['bh']:+7.2%} "
                          f"回撤={r['dd']:+7.2%} ({time.time() - t0:.0f}s)", flush=True)
                # 换池前清缓存：ParquetProvider 常驻整张 82 列日线（universe 800 约
                # 1.3 GB），三个 offset 攒起来会把机器吃满；分位表同理（每格 20 万条）。
                provider._cache.clear()
                _RANKS.clear()
            if gate_rules is None:
                control_cells = cells
            results.append(_summarize(name, cfg, arm_label, cells, control_cells, args))

    print(render(results, args))


def _summarize(name: str, cfg, arm: str, cells: list[tuple], control_cells: list[tuple],
               args) -> dict:
    wrs = [c[2]["wr"] for c in cells]
    pays = [_scan._num(c[2]["payoff"], 99.0) for c in cells]
    pfs = [_scan._num(c[2]["pf"], 99.0) for c in cells]
    exps = [c[2]["exp"] for c in cells]
    ns = [c[2]["n"] for c in cells]
    per_cell_ok = [bool(n >= args.min_trades and p >= args.payoff_target and w >= args.wr_target
                        and pf >= args.pf_target and e > 0)
                   for n, p, w, pf, e in zip(ns, pays, wrs, pfs, exps)]
    rec = {
        "name": name, "cfg": cfg, "arm": arm, "cells": cells,
        "wr_min": min(wrs), "wr_max": max(wrs),
        "payoff_min": min(pays), "payoff_max": max(pays),
        "pf_min": min(pfs), "pf_max": max(pfs),
        "exp_min": min(exps), "exp_max": max(exps),
        "n_min": min(ns), "n_max": max(ns),
        "pass_cells": sum(per_cell_ok), "pass": all(per_cell_ok),
    }
    if arm != "对照(无门)" and control_cells:
        # 配对差值：同一格内闸门臂减对照臂。两臂跑的是同一次循环的同格、同一批股票，
        # 唯一差别是闸门，所以下标一一对应即可（顺序由 run 的循环保证一致）。
        rec.update(_paired(cells, control_cells))
    return rec


def _paired(cells: list[tuple], ctl: list[tuple]) -> dict:
    """与对照臂逐格相减。市场水位、时代涨跌在同格内被抵消，剩下的才是闸门的作用。"""
    dwr = [c[2]["wr"] - k[2]["wr"] for c, k in zip(cells, ctl)]
    dpay = [_scan._num(c[2]["payoff"], 0.0) - _scan._num(k[2]["payoff"], 0.0) for c, k in zip(cells, ctl)]
    dpf = [_scan._num(c[2]["pf"], 0.0) - _scan._num(k[2]["pf"], 0.0) for c, k in zip(cells, ctl)]
    dexp = [c[2]["exp"] - k[2]["exp"] for c, k in zip(cells, ctl)]
    def stable(xs: list[float]) -> bool:
        """九格同号：全正或全负才算方向一致；有一格反号就当噪声，不当"基本同号"。"""
        return bool(min(xs) > 0 or max(xs) < 0)

    return {
        "d_wr": (min(dwr), max(dwr)), "d_wr_mean": sum(dwr) / len(dwr), "wr_stable": stable(dwr),
        "d_payoff": (min(dpay), max(dpay)), "payoff_stable": stable(dpay),
        "d_pf": (min(dpf), max(dpf)),
        "d_exp": (min(dexp), max(dexp)), "d_exp_mean": sum(dexp) / len(dexp),
        "exp_stable": stable(dexp),
        "ctl_n_min": min(k[2]["n"] for k in ctl), "ctl_n_max": max(k[2]["n"] for k in ctl),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="因子分位闸门回测")
    ap.add_argument("--strategies", default="pattern_reversal")
    ap.add_argument("--eras", default=",".join(_scan.DEFAULT_ERAS))
    ap.add_argument("--offsets", default="0,137,274")
    ap.add_argument("--universe", type=int, default=250)
    ap.add_argument("--exits", default="0.05:0.00:10,0.04:0.10:15")
    ap.add_argument("--rules", default="", help="手工指定规则 特征:箱号[,…]，留空则自动取筛选表候选")
    ap.add_argument("--mode", choices=["single", "union", "all"], default="single",
                    help="single=每条规则单独一臂；union=任一规则命中即放行；all=全部规则都命中才放行")
    ap.add_argument("--max-candidates", type=int, default=6)
    ap.add_argument("--capital", type=float, default=100000.0)
    ap.add_argument("--max-positions", type=int, default=5)
    ap.add_argument("--position-cap-pct", type=float, default=0.2)
    ap.add_argument("--target-amount", type=float, default=15000.0)
    ap.add_argument("--payoff-target", type=float, default=2.0)
    ap.add_argument("--wr-target", type=float, default=0.50)
    ap.add_argument("--pf-target", type=float, default=2.0)
    ap.add_argument("--min-trades", type=int, default=30)
    ap.add_argument("--audit", action="store_true")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
