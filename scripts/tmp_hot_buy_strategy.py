"""临时诊断 2：把「买热门」做成真策略，和线上模型同一把尺子比。

规则（每 5 个交易日 rebalance 一次，等权 10 只）：
- 热门臂：20 日涨幅前 10（可买的）；对照加一臂「热门且站上 MA20」。
- 模型臂：pred_live2026jun.parquet 分数前 10。
- 执行：rebalance 日开盘价买入，5 个交易日后下一 rebalance 日开盘价卖出（换手=每 5 日全换）。
- 成本：买 0.05% + 卖 0.15% = 单回合 0.20%（与线上回测 EXCHANGE_KWARGS 一致）。
- 基准：SH510300 同一 rebalance 网格、同开盘对开盘口径。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import qlib
from qlib.data import D

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
QLIB_DIR = Path("/data/work/qlib_data/cn")
POOL = (REPO / "data/qlib/model_batchQ_lab5/pool.txt").read_text().split()
WARM, BEG, END = "2022-09-01", "2024-01-02", "2026-10-15"   # 批次 Q 窗
COST_RT = 0.002
STEP = 5


def main() -> int:
    qlib.init(provider_uri=str(QLIB_DIR), region=qlib.constant.REG_CN, kernels=1)
    df = D.features(POOL, ["$close", "$open"], start_time=WARM, end_time=END)
    close = df["$close"].unstack("instrument")
    o = df["$open"].unstack("instrument")
    days = close.loc[BEG:END].index
    ma20 = close.rolling(20).mean()
    mom20 = close / close.shift(20) - 1.0

    # 线上 live2026jun 的 pred 只落盘了 2026-07 起的服务段；用批次 Q 的 T+5 臂
    # （与线上同口径的 5 日标签研究臂）作模型对照。
    pred = pd.read_parquet(REPO / "data/qlib/pred_batchQ_lab5.parquet")
    pred["datetime"] = pd.to_datetime(pred["datetime"])
    score = pred.set_index(["datetime", "instrument"])["score"].sort_index()

    bench = D.features(["SH510300"], ["$close"], start_time=BEG, end_time=END)["$close"].xs("SH510300", level="instrument")

    grids = list(range(0, len(days) - STEP, STEP))
    recs = {k: [] for k in ["热门前10", "热门前10+MA20", "模型前10", "基准"]}
    leg_days = []   # 每段腿的起点日，用于把各臂对齐到同一公共区间
    picks = {k: [] for k in recs}
    for gi, idx in enumerate(grids):
        d, dn = days[idx], days[idx + STEP]
        # 信号必须只用 d 日开盘前可得的信息：用 d-1 日收盘算动量/均线（避免前视）
        d_sig = days[idx - 1] if idx >= 1 else d
        mo, c, m2, op = mom20.loc[d_sig], close.loc[d_sig], ma20.loc[d_sig], o.loc[d]
        nxt_op = o.loc[dn]
        ok = mo.notna() & c.notna() & op.notna()
        if ok.sum() < 10:
            continue
        leg_days.append(dn)

        def basket(names: pd.Index, tag: str):
            r = (nxt_op[names] / op[names] - 1.0).dropna()
            recs[tag].append(r.mean() - COST_RT if len(r) else np.nan)
            picks[tag].append(len(r))

        hot = mo[ok].nlargest(10).index
        basket(hot, "热门前10")
        hot2 = mo[ok & (c >= m2.fillna(False))].nlargest(10).index
        basket(hot2, "热门前10+MA20")
        try:
            sd = score.xs(d, level="datetime")
        except KeyError:
            continue
        mod = sd.sort_values(ascending=False).head(10).index
        mod = [s for s in mod if s in ok.index and bool(ok.get(s, False))]
        basket(pd.Index(mod), "模型前10")
        try:
            recs["基准"].append(float(bench.loc[dn] / bench.loc[d] - 1.0) - COST_RT)
        except KeyError:
            recs["基准"].append(np.nan)

    print(f"=== rebalance 网格（每段 5 日、扣往返 0.20%）{BEG}→{END}，信号取入场前一日收盘 ===")
    ends = leg_days
    common = ends[:len(recs["基准"])]
    common = common[:min(len(v) for v in recs.values())]
    lo, hi = common[0], common[-1]
    print(f"各臂对齐到同一批腿：{len(common)} 段，{common[0].date()} → {common[-1].date()}")
    print(f"{'臂':14s} {'段均净收益':>10s} {'段胜率':>8s} {'净值(累计)':>10s} {'最大回撤':>9s}")
    for name, series in recs.items():
        s = pd.Series(series[:len(common)], index=common).dropna()
        nav = (1 + s).cumprod()
        dd = (nav / nav.cummax() - 1).min()
        yrs = pd.Series(s.values, index=pd.DatetimeIndex(common)).groupby(lambda x: x.year).apply(lambda z: (1 + z).prod() - 1)
        print(f"{name:14s} {s.mean():>+10.3%} {(s>0).mean():>8.1%} {nav.iloc[-1]:>10.3f} {dd:>9.1%}  "
              f"分年 " + " ".join(f"{y}:{r:+.1%}" for y, r in yrs.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
