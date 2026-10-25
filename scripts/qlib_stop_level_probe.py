"""qlib 榜单能不能给「失效位」：把价位止损当场测一遍（docs §27 那三个数的复现脚本）。

用法：
    python3 -u scripts/qlib_stop_level_probe.py

口径（读数时必须一起给）：
- 名单 = 线上那只模型 `batchQ_lab10` 的留出段档案 `data/qlib/pred_batchQ_lab10.parquet`
  （300 只训练池 / 2024-01-02→2026-10-16 共 673 个信号日 / 每天取分数前 30 只 = App 那张名单）。
  池子是 300 只训练池，**不是**线上现在打分用的 2,835 只全市场池。
- 入场价 = 信号日收盘，也就是 App 卡片上的「基准价」（回测口径同样是它）。
- A 股 T+1：最早 T+1 才能卖，所以止损从 T+1 起逐日看盘中最低价。
- 触线按「线价成交」记——跳空低开时真实成交更差，所以本脚本的止损臂读数**偏乐观**。
- 毛收益、未扣成本；超额 = 减当日全池（300 只）等权 10 日均值。
- 逐日窗口重叠 10 天，所以配对 t 要 ÷√10 才接近真实读数。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from stock_selector_qlib import artifacts, serving  # noqa: E402

HOLD = 10          # 与线上标签 Ref($close,-10) 同长
TOP = 30           # App 一屏的名单长度
LEVELS = (0.03, 0.05, 0.08, 0.10, 0.15, 0.20)


def main() -> int:
    art = artifacts.load()
    serving.init_qlib()
    pred = pd.read_parquet(REPO / "data" / "qlib" / "pred_batchQ_lab10.parquet")
    pred["datetime"] = pd.to_datetime(pred["datetime"])
    days = sorted(pred["datetime"].unique())
    beg, end = pd.Timestamp(days[0]), pd.Timestamp(days[-1])
    pool = sorted(pred["instrument"].unique())
    print(f"档案：{art.run_id}（train_end {art.meta.get('train_end')}）  "
          f"{len(pool)} 只  {len(days)} 个信号日（{beg.date()}→{end.date()}）")

    import qlib.data

    cal = serving.calendar()
    tail_end = cal[min(len(cal) - 1, cal.index(str(end.date())) + HOLD + 2)]
    frame = qlib.data.D.features(pool, ["$close", "$low"],
                                 start_time=str(beg.date()), end_time=tail_end)
    wide = {}
    for field in ("$close", "$low"):
        w = frame[field].unstack("instrument")
        w.index = pd.to_datetime(w.index)
        wide[field] = w.sort_index()
    closes, lows = wide["$close"], wide["$low"]

    scores = pred.pivot(index="datetime", columns="instrument", values="score")
    pos = {d: i for i, d in enumerate(closes.index)}
    rows = []
    for d in scores.index:
        if d not in pos or pos[d] + HOLD >= len(closes.index):
            continue
        i = pos[d]
        top = scores.loc[d].dropna().sort_values(ascending=False).index[:TOP]
        picked = [c for c in top if not np.isnan(closes.iloc[i][c])]
        if not picked:
            continue
        entry = closes.iloc[i][picked]
        hold_ret = (closes.iloc[i + HOLD][picked] / entry - 1.0)
        deep = lows.iloc[i:i + HOLD][picked].min() / entry - 1.0
        base = float((closes.iloc[i + HOLD] / closes.iloc[i] - 1.0).mean())
        rows.append({"date": d, "picked": picked, "entry": entry,
                     "hold": hold_ret, "deep": deep, "base": base,
                     "lows": lows.iloc[i + 1:i + HOLD + 1][picked]})
    print(f"可评估：{len(rows)} 个信号日 × 前 {TOP} 名 = "
          f"{sum(len(r['picked']) for r in rows):,} 个「日·票」")

    dips = pd.Series(np.concatenate([[v for v in r["deep"].values] for r in rows]))
    dips = dips[dips == dips]
    print(f"\n=== 底色：名单内 10 日最深下探 ===")
    print(f"    中位 {dips.median():+.1%}  均值 {dips.mean():+.1%}  "
          f"10 分位 {dips.quantile(0.10):+.1%}  25 分位 {dips.quantile(0.25):+.1%}")
    hold_ex = pd.Series({(r["date"], c): r["hold"][c] - r["base"]
                         for r in rows for c in r["picked"] if r["hold"][c] == r["hold"][c]})
    print(f"    不设止损、拿满 10 日的平均超额 {hold_ex.mean():+.2%}"
          f"（{len(hold_ex):,} 笔，毛收益未扣成本）")

    print(f"\n=== 价位止损 vs 拿满 10 日 ===")
    print(f"{'止损线':>7} {'触发率':>7} {'配对差/笔':>10} {'t':>7} {'t÷√10':>7} "
          f"{'带止损超额':>11} {'止损后拿满更赚':>13}")
    for x in LEVELS:
        diffs, excess, fired, rec = [], [], 0, []
        for r in rows:
            barrier = r["entry"] * (1 - x)
            hit = r["lows"].le(barrier, axis=1)
            for code in r["picked"]:
                h = r["hold"].get(code)
                if h is None or h != h:
                    continue
                if not hit[code].any():
                    diffs.append(0.0)
                    excess.append(h - r["base"])
                    continue
                fired += 1
                stop_ret = -x
                diffs.append(stop_ret - h)
                excess.append(stop_ret - r["base"])
                rec.append(h - stop_ret)
        s = pd.Series(diffs)
        std = float(s.std())
        t = float(s.mean()) / std * np.sqrt(len(s)) if std else float("nan")
        print(f"   -{x:<4.0%} {fired / len(s):>7.1%} {s.mean():>+9.2%} {t:>7.2f} "
              f"{t / np.sqrt(HOLD):>7.2f} {pd.Series(excess).mean():>+10.2%} "
              f"{np.mean([v > 0 for v in rec]) if rec else float('nan'):>12.1%}"
              f"（被止损 {len(rec):,} 笔，平均差 {np.mean(rec) if rec else float('nan'):+.2%}）")
    print("\n读法：配对差全列为负 ⇒ 每一档价位止损都比拿满 10 日差；"
          "t÷√10 到 −2 以下的说明不只是噪声。止损线设得越浅，被洗出去的好票越多。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
