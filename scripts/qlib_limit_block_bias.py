"""取消涨跌停挡单的偏差有多大：拿线上臂 batchW_c3 的头 30，按新旧两套撮合口径各数一遍。

qlib 的挡单判据（`qlib/backtest/exchange.py::_update_limit`，float 阈值分支）：
  limit_buy  = $change >= 0.095   （当天**收盘**涨过 9.5% → 整天买不进）
  limit_sell = $change <= -0.095  （当天收盘跌过 9.5% → 整天卖不掉）
它看的是当天涨跌幅，不是「一字板」——所以旧口径比真实盘口更严，被挡掉的绝大多数是
**当天大涨/大跌但盘中明明能成交**的票。取消挡单（2026-10-29 用户拍板）改的就是这一条。

腿与 arms 表同尺：batchW_c3 的可执行腿 = 信号日 F 的次日 T+1 开盘买、当天收盘卖（E1）。
"""
from __future__ import annotations

import warnings

import pandas as pd
import qlib
from qlib.data import D

warnings.filterwarnings("ignore")

PRED = "data/qlib/pred_batchW_c3.parquet"
WIN_S, WIN_E = "2024-01-02", "2026-10-23"
TOPK, THRESH = 30, 0.095


def main() -> None:
    qlib.init(provider_uri="/data/work/qlib_data/cn", kernels=1)
    pred = pd.read_parquet(PRED)
    insts = sorted(pred["instrument"].unique())
    # 300 只一把抓会掉日期（arms 表同一坑），分片取再合并
    px = pd.concat([D.features(insts[i:i + 60], ["$close", "$open", "$high", "$low"],
                               start_time="2024-01-01", end_time="2026-10-28")
                    for i in range(0, len(insts), 60)])
    close = px["$close"].unstack("instrument")
    open_ = px["$open"].unstack("instrument")
    high = px["$high"].unstack("instrument")
    low = px["$low"].unstack("instrument")
    prev = close.shift(1)
    change = close / prev - 1.0                     # 当天收盘相对昨收 = qlib 的 $change
    e1 = close / open_ - 1.0                        # T+1 开盘买、当天收盘卖
    # 真实盘口能不能成交：整天最低价都在涨停价之上 ⇒ 一字/封死，确实买不进
    sealed_buy = low >= prev * (1 + THRESH)
    sealed_sell = high <= prev * (1 - THRESH)
    days = list(close.index)
    pos = {d: i for i, d in enumerate(days)}

    pred["datetime"] = pd.to_datetime(pred["datetime"])
    by_day = {d: g.set_index("instrument")["score"] for d, g in pred.groupby("datetime")}
    rows = []
    for d, g in by_day.items():
        if d not in pos or pos[d] + 1 >= len(days):
            continue
        head = g.nlargest(TOPK)
        t1 = days[pos[d] + 1]
        for inst in head.index:
            rows.append((t1, inst, float(close.at[t1, inst]), float(change.at[t1, inst]),
                         float(e1.at[t1, inst]), bool(sealed_buy.at[t1, inst]),
                         bool(sealed_sell.at[t1, inst])))
    df = pd.DataFrame(rows, columns=["leg_day", "instrument", "close", "change", "e1",
                                     "sealed_buy", "sealed_sell"])
    df = df.dropna(subset=["close"])                 # 停牌/缺数：新旧口径都不成交
    n = len(df)
    buy_blocked = df["change"] >= THRESH
    sell_blocked = df["change"] <= -THRESH
    leg_day = pd.to_datetime(df["leg_day"])
    years = (leg_day.max() - leg_day.min()).days / 365.25

    def stat(mask: pd.Series, label: str) -> None:
        sub = df[mask]
        print(f"{label}: {len(sub)}/{n} 笔（{len(sub)/n*100:.2f}%，约 {len(sub)/years:.0f} 笔/年），"
              f"E1 均值 {sub['e1'].mean()*100:+.3f}%，中位 {sub['e1'].median()*100:+.3f}%")

    print(f"[样本] batchW_c3 头 {TOPK}，腿日 {leg_day.min():%Y-%m-%d}→{leg_day.max():%Y-%m-%d}"
          f"（{years:.2f} 年），{df['leg_day'].nunique()} 个腿日 / {n} 笔 pick-day")
    stat(buy_blocked, "旧口径买不进（腿日收盘≥+9.5%）")
    stat(buy_blocked & df["sealed_buy"], "  其中封死一天、真买不进")
    stat(buy_blocked & ~df["sealed_buy"], "  其中盘中买得到（旧口径挡错）")
    stat(sell_blocked, "旧口径卖不掉（腿日收盘≤−9.5%）")
    stat(sell_blocked & df["sealed_sell"], "  其中跌停封死、真卖不掉")
    stat(~buy_blocked & ~sell_blocked, "两口径都能成交")
    all_mean = df["e1"].mean()
    old_mean = df[~(buy_blocked | sell_blocked)]["e1"].mean()
    print(f"[净偏差] 新口径把 {(buy_blocked | sell_blocked).sum()} 笔原被挡的腿按成交计入："
          f"全体 {all_mean*100:+.3f}% vs 旧口径（只算能成交）{old_mean*100:+.3f}% "
          f"⇒ 每笔偏乐观 {100*(all_mean-old_mean):+.3f}pp")


if __name__ == "__main__":
    main()
