"""头名选股的前向收益拆分：MA5 向上 vs 不向上，谁更赚钱。

回答 2026-10-25 的追问：「排名第一是趋势向下的，说明模型有问题；趋势向上的才有买的价值」。
这个命题可以不吵——直接看数：模型当天的头名（分数最高那只，动态池+护栏后的）
按「MA5 向上/不向上」分两组，比较**之后 5 个交易日**的涨跌（收盘→5 日后收盘）。

- 信息集：判定只用信号日 F 收盘前的数据（close_F、MA5_F、MA5_{F−1}），无未来函数；
  收益端用 F 之后的数据，属于"事后检验"本来就要用的。
- 超额口径：fwd5 减同期基准（SH510300）同样 5 日收益。
- 候选范围 = 每天打完后护栏剩下的名单（signal 文件），top-50 = 分数前 50（= entry_rank）。

用法：python3 -u scripts/qlib_ma5_forward.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
for p in (str(REPO), str(SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

import qlib_fullmarket_validation as fv  # noqa: E402
from stock_selector_qlib import serving  # noqa: E402
from stock_selector_qlib.backtest import _snap_window  # noqa: E402

SIGNAL = REPO / "data" / "qlib" / "fullmarket_validation_20261025_signal_st.parquet"
OUT = REPO / "data" / "qlib" / "ma5gate_forward_20261025.json"
START, END = "2024-01-02", "2026-10-22"

def main() -> int:
    serving.init_qlib()
    days = serving.calendar()
    start, end = _snap_window(days, START, END)
    sig = pd.read_parquet(SIGNAL)["score"].loc[start:end]
    pool = sorted(set(sig.index.get_level_values("instrument")))
    print(f"窗口 {start}→{end}，信号 {len(sig):,} 行", flush=True)

    close, _ = fv.load_panel(pool, days)
    ma5 = close.rolling(5, min_periods=5).mean()
    ok = (close > ma5) & (ma5 > ma5.shift(1))
    del ma5

    idx, cols = close.index, close.columns
    pos_d = idx.get_indexer(sig.index.get_level_values("datetime"))
    pos_s = cols.get_indexer(sig.index.get_level_values("instrument"))
    assert (pos_d >= 0).all() and (pos_s >= 0).all(), "信号不在面板坐标系里，查口径"
    gate = ok.to_numpy()[pos_d, pos_s]

    fwd = (close.shift(-5) / close - 1).to_numpy()
    val = fwd[pos_d, pos_s]
    bench = fv.benchmark_curve("SH510300", start, end)
    bval = (bench / bench.shift(-5) - 1).reindex(idx).to_numpy()[pos_d]

    frame = sig.reset_index()
    top_pos = frame.groupby("datetime")["score"].idxmax().to_numpy()
    head = np.zeros(len(sig), dtype=bool)
    head[top_pos] = True

    # 每日 top-50（= entry_rank 的候选集合）
    order = frame.sort_values(["datetime", "score"], ascending=[True, False], kind="mergesort")
    top50_rows = order.index.to_numpy()[(order.groupby("datetime").cumcount() < 50).to_numpy()]
    top50 = np.zeros(len(sig), dtype=bool)
    top50[top50_rows] = True

    fin = np.isfinite(val) & np.isfinite(bval)
    print("（缺 5 日后行情的行丢出统计，不编造成绩）", flush=True)

    def grp(mask: np.ndarray) -> dict:
        m = mask & fin
        if not m.any():
            return {"n": 0}
        e = val[m] - bval[m]
        return {
            "n": int(m.sum()),
            "fwd5_mean": round(float(val[m].mean()), 4),
            "fwd5_median": round(float(np.median(val[m])), 4),
            "fwd5_win": round(float((val[m] > 0).mean()), 4),
            "excess5_mean": round(float(e.mean()), 4),
        }

    open50_days = pd.DataFrame({"day": sig.index.get_level_values("datetime").to_numpy()[top50_rows],
                                "open": gate[top50_rows]})
    per_day = open50_days.groupby("day")["open"].sum()
    stats = {
        "window": [start, end],
        "gate_def": "close_F > MA5_F 且 MA5_F > MA5_{F-1}（信号日 F 收盘前信息集）",
        "forward": "收盘→5 个交易日后收盘；基准 SH510300 同口径",
        "days": int(per_day.shape[0]),
        "top50_with_zero_up_days": int((per_day == 0).sum()),
        "top50_mean_up_per_day": round(float(per_day.mean()), 1),
        "groups": {
            "头名_MA5向上": grp(head & (gate == 1)),
            "头名_MA5不向上": grp(head & (gate == 0)),
            "top50_MA5向上": grp(top50 & (gate == 1)),
            "top50_MA5不向上": grp(top50 & (gate == 0)),
            "top50内最优MA5向上(次日B臂买的)": grp(top50 & (gate == 1) & ~head),
            "全部信号行_MA5向上": grp((gate == 1)),
            "全部信号行_MA5不向上": grp((gate == 0)),
        },
    }
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    OUT.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"产物：{OUT}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
