"""两臂配对检验：把两次滚动训练落在**同一批交易日**上的逐日读数相减，检验差异能不能归给「换训练窗」。

为什么必须有这一件事：`train_end` 一挪，点估计 IC 会从 0.0358 跳到 0.0491（批次 I 实测 +37%），
但那是两条各自抖动的序列比平均值——票池同时还换了血（59/300），窗口又高度重叠，
单看这两个数无法区分「模型真变好」和「同期市场本来就好」。配对检验在同一天内部相减，
把那一天的共同行情抵消掉，剩下的才是两臂的差。

输入是 `scripts/qlib_run.py` 落盘的 `data/qlib/ic_daily_<run_id>.parquet`（逐日宽表）。
默认比 `spearman_test`（测试段逐日秩相关，主读数），也可以比 `ls_spread`（Q5−Q1 同日价差）。

**必须 ÷√5**：标签是 `Ref($close,-5)/Ref($close,-1)-1`，相邻 5 个交易日的标签共用同一段涨跌，
逐日序列是自相关的，普通 t 会系统性高估显著性。读数出来先除以 √重叠天数（默认 5），
`--overlap 1` 才是关掉这个修正（只在你确认序列不重叠时用）。

用法:
    python3 scripts/qlib_ic_pair.py live2024 reroll_2026H2
    python3 scripts/qlib_ic_pair.py live2024 reroll_2026H2 --column ls_spread --json
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import pandas as pd

REPORT_DIR = Path("/data/work/SelectStock/data/qlib")


def ic_path(run_id: str, directory: Path) -> Path:
    """按命名约定找文件；找不到就把同目录里实际有哪些报出来，免得拿错 run_id 空跑。"""
    path = directory / f"ic_daily_{run_id}.parquet"
    if not path.exists():
        have = sorted(p.name.removeprefix("ic_daily_").removesuffix(".parquet")
                      for p in directory.glob("ic_daily_*.parquet"))
        raise FileNotFoundError(f"没有 {path.name}。已有逐日读数：{have or '（一份都没有）'}")
    return path


def load_column(run_id: str, column: str, directory: Path = REPORT_DIR) -> pd.Series:
    frame = pd.read_parquet(ic_path(run_id, directory))
    if column not in frame.columns:
        raise KeyError(f"{run_id} 里没有列 {column}，可选：{list(frame.columns)}")
    series = frame[column].astype(float)
    series.index = pd.to_datetime(series.index)
    return series.dropna().sort_index()


def pair_test(a: pd.Series, b: pd.Series, overlap: int = 5, min_days: int = 60) -> dict:
    """两臂按日配对：返回原始 t 与 ÷√overlap 之后的 t。

    只取**两边都有数**的日子——一边缺一天的话，配对差就退化成单边值，
    那几天的差异完全来自「谁那天有行情」，会把均值往随机方向拽。
    """
    day = a.index.intersection(b.index)
    x, y = a.reindex(day), b.reindex(day)
    diff = (x - y).dropna()
    n = int(len(diff))
    xa, yb = x.reindex(diff.index), y.reindex(diff.index)
    # 一边是常数时 corr 会走 0/0（numpy 直接 RuntimeWarning），如实给 NaN 而不是让警告刷屏
    corr = float("nan") if (xa.std() < 1e-12 or yb.std() < 1e-12) else float(xa.corr(yb))
    out = {
        "days_a": int(len(a)),
        "days_b": int(len(b)),
        "days_paired": n,
        "window": [str(day.min())[:10], str(day.max())[:10]] if n else None,
        "mean_a": round(float(xa.mean()), 6),
        "mean_b": round(float(yb.mean()), 6),
        "mean_diff": round(float(diff.mean()), 6),
        "corr_ab": round(corr, 4),
        "overlap": overlap,
        "share_diff_positive": round(float((diff > 0).mean()), 4) if n else None,
        "enough_days": n >= min_days,
    }
    sd = float(diff.std())
    # 常数序列的浮点标准差不是 0 而是 ~1e-18，直接比 >0 会算出 1e16 这种假 t
    if n >= 2 and sd > 1e-12:
        t_raw = float(diff.mean() / (sd / math.sqrt(n)))
        out["t_raw"] = round(t_raw, 3)
        out["t_adjusted"] = round(t_raw / math.sqrt(overlap), 3)
    else:
        out["t_raw"] = out["t_adjusted"] = float("nan")
        out["note"] = "两臂逐日读数完全相同或样本不足：没有波动就没有可检验的差异"
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_a", help="臂 A 的 run_id（约定上是被减数：diff = A − B）")
    ap.add_argument("run_b", help="臂 B 的 run_id")
    ap.add_argument("--column", default="spearman_test",
                    help="逐日宽表里要比的列（spearman_test / pearson_test / ls_spread…）")
    ap.add_argument("--overlap", type=int, default=5,
                    help="标签重叠天数，t 要 ÷√它；本链路标签固定 5 日")
    ap.add_argument("--min-days", type=int, default=60,
                    help="配对天数低于它就只报点估计、不给结论")
    ap.add_argument("--dir", default=str(REPORT_DIR), type=Path)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    a = load_column(args.run_a, args.column, args.dir)
    b = load_column(args.run_b, args.column, args.dir)
    result = pair_test(a, b, overlap=args.overlap, min_days=args.min_days)
    result.update({"run_a": args.run_a, "run_b": args.run_b, "column": args.column})
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    print(f"配对 {result['column']}：{args.run_a} − {args.run_b}，{result['days_paired']} 天"
          f"（{result['window']}）")
    for key in ("mean_a", "mean_b", "mean_diff", "corr_ab", "t_raw", "t_adjusted",
                "share_diff_positive"):
        print(f"  {key:22s} {result[key]}")
    if "note" in result:
        print(f"  {result['note']}")
    if not result["enough_days"]:
        print(f"  天数不足 {args.min_days}：只当点估计，别写成结论")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
