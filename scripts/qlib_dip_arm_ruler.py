"""板后低吸臂的判决尺子：不看全市场横截面 IC（标签 99% 是 0，那个数没有意义），
只看**它真要做的那笔交易**：今天板 ∩ 明开/今收 < 0.95 ⇒ 明早开盘买、当天收盘卖（E1）。

三问：
① 触发事件有多少个（样本薄不薄，先摆出来）；
② 按臂的分数在触发事件里排序，头部是不是真的比全体强（同日配对 t）；
③ 线上榜 batchW_c3 用同一把尺子量，两臂同尺对照，差别只归因到标签。

用法：python3 scripts/qlib_dip_arm_ruler.py pred_batchG_u1.parquet [pred_batchW_c3.parquet ...]
"""
from __future__ import annotations

import sys
import warnings

import numpy as np
import pandas as pd
import qlib
from qlib.data import D

warnings.filterwarnings("ignore")

WIN_S, WIN_E = "2024-01-01", "2026-10-28"
BOARD = 0.0975       # 今天板：今收/昨收 − 1 ≥ 9.75%（主板 10% 口径，留 0.25pp 容差）
GAP = -0.05          # 明开/今收 − 1 < −5%（= 明开/今收 < 0.95；2026-10-29 取消 −9.5% 下界）
HEADS = (5, 10, 20)  # 每天按分数取前 N 个触发事件


def panel(insts: list[str]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    px = pd.concat([D.features(insts[i:i + 60], ["$close", "$open"],
                               start_time=WIN_S, end_time=WIN_E)
                    for i in range(0, len(insts), 60)])
    close = px["$close"].unstack("instrument")
    open_ = px["$open"].unstack("instrument")
    return close, open_, close.shift(1)      # 昨收用 close.shift(1)（$pre_close 在本面板不可用）


def leg(close: pd.DataFrame, open_: pd.DataFrame) -> pd.DataFrame:
    """信号日 F 的下一天：入场=次日开盘，出场=次日收盘（E1）。"""
    nxt_open = open_.shift(-1)
    nxt_close = close.shift(-1)
    return nxt_close / nxt_open - 1.0


def judge(name: str, pred_file: str, close, open_, prev) -> None:
    pred = pd.read_parquet(f"data/qlib/{pred_file}")
    pred["datetime"] = pd.to_datetime(pred["datetime"])
    days = list(close.index)
    pos = {d: i for i, d in enumerate(days)}

    recs = []
    for d, g in pred.groupby("datetime"):
        if d not in pos or pos[d] + 2 >= len(days):
            continue
        t = pos[d]
        chg = close.iloc[t] / prev.iloc[t] - 1.0            # 今天的涨幅（板门）
        gap = open_.iloc[t + 1] / close.iloc[t] - 1.0       # 明开/今收
        e1 = leg(close, open_).iloc[t]
        today = g.set_index("instrument")["score"]
        for inst in today.index:
            c, gp, r = chg.get(inst), gap.get(inst), e1.get(inst)
            if not (pd.notna(c) and pd.notna(gp) and pd.notna(r)):
                continue
            trig = (c >= BOARD) and (gp < GAP)
            if trig:
                recs.append((d, inst, float(today[inst]), float(r)))
    ev = pd.DataFrame(recs, columns=["day", "inst", "score", "e1"])
    print(f"\n=== {name}（{pred_file}）===")
    print(f"① 触发事件 {len(ev)} 笔（{ev['day'].nunique()} 个交易日，"
          f"{len(ev)/max(1, ev['day'].nunique()):.2f} 笔/日），全体 E1 均值 "
          f"{ev['e1'].mean()*100:+.3f}%、中位 {ev['e1'].median()*100:+.3f}%、"
          f"胜率 {(ev['e1'] > 0).mean()*100:.1f}%")
    if len(ev) < 10:
        print("   样本 <10 笔，后面的排序读数不成立，如实说明。")
        return
    ev = ev.sort_values(["day", "score"], ascending=[True, False])
    base = ev["e1"].mean()
    for n in HEADS:
        head = ev.groupby("day", sort=False).head(n)
        diff = head.groupby("day")["e1"].mean() - ev.groupby("day")["e1"].mean()
        tstat = (diff.mean() / (diff.std(ddof=1) / np.sqrt(len(diff)))
                 if len(diff) > 2 and diff.std(ddof=1) > 0 else float("nan"))
        print(f"② 每天分数前 {n:>2} 笔：E1 均值 {head['e1'].mean()*100:+.3f}% "
              f"（全体 {base*100:+.3f}%）差 {(head['e1'].mean()-base)*100:+.3f}pp，"
              f"同日配对 {diff.mean()*100:+.3f}pp t {tstat:+.2f}（{len(diff)} 天有两头可比）")


def main() -> None:
    qlib.init(provider_uri="/data/work/qlib_data/cn", kernels=1)
    files = sys.argv[1:] or ["pred_batchG_u1.parquet", "pred_batchW_c3.parquet"]
    insts = sorted({i for f in files for i in
                    pd.read_parquet(f"data/qlib/{f}")["instrument"].unique()})
    close, open_, prev = panel(insts)
    print(f"[面板] {len(insts)} 只 / {close.shape[0]} 个交易日 {WIN_S}→{WIN_E}")
    for f in files:
        judge(f.replace("pred_", "").replace(".parquet", ""), f, close, open_, prev)


if __name__ == "__main__":
    main()
