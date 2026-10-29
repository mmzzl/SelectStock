"""批次 H 的基准尺子：他这条规则**不带模型**跑起来是什么样子，以及「按模型分取头」
比「同人口里随机取 5 只」到底强多少（安慰剂对照，批次 Z 的教训：随机剔 20% 比按分剔更好，
所以这次先量随机）。

只读日线面板（close/open/low），不建 Alpha158 ⇒ 900 只 10 年约 40 秒。

用法：python3 scripts/qlib_rule_baseline.py [--pool liq900] [--test-beg 2024-01-01]
"""
from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import qlib
from qlib.data import D

warnings.filterwarnings("ignore")
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from stock_selector_qlib.dip_rule import rule_event_rows  # noqa: E402

WIN_S, WIN_E = "2016-01-01", "2026-10-16"
CHUNK = 150
COST = 0.0005 + 0.0015
BUCKETS = ((0.05, 0.07, "涨 5~7%"), (0.07, 0.0975, "涨 7~9.75%"),
           (0.0975, 0.20, "涨停板"), (0.20, 9.99, "涨超 20%（含 20cm/ST）"))


def paired(head: pd.DataFrame, ev: pd.DataFrame, col: str = "ret") -> tuple[float, float]:
    """同日配对：头 N 的均值减当天全体均值，返回 (差 pp, t)。"""
    diff = head.groupby("day")[col].mean() - ev.groupby("day")[col].mean()
    sd = diff.std(ddof=1)
    t = float(diff.mean() / (sd / np.sqrt(len(diff)))) if sd > 0 else float("nan")
    return float(diff.mean() * 100), t


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", default="liq900.txt")
    ap.add_argument("--test-beg", default="2024-01-01")
    ap.add_argument("--score", default="pred_batchH_r1.parquet")
    args = ap.parse_args()

    qlib.init(provider_uri="/data/work/qlib_data/cn", kernels=1)
    syms = [ln.split("\t")[0] for ln in
            Path(f"/data/work/qlib_data/cn/instruments/{args.pool}").read_text().splitlines()
            if ln.strip()]
    parts = []
    for i in range(0, len(syms), CHUNK):
        batch = syms[i:i + CHUNK]
        px = D.features(batch, ["$close", "$open", "$low"], start_time=WIN_S, end_time=WIN_E)
        parts.append(rule_event_rows(px.rename(columns={"$close": "close", "$open": "open",
                                                        "$low": "low"})))
    ev = pd.concat(parts).sort_index()
    ev["day"] = ev.index.get_level_values("datetime")
    ev["ret"] = ev["label"]
    print(f"[人口] {args.pool} {len(syms)} 只，{WIN_S}→{WIN_E}，事件 {len(ev)} 笔 / "
          f"{ev['day'].nunique()} 个交易日")

    test = ev[ev["day"] >= pd.Timestamp(args.test_beg)]
    print(f"\n=== 规则本身（不带模型）·样本外 {args.test_beg}→2026-10-16：{len(test)} 笔 "
          f"{test['day'].nunique()} 日 ===")
    print(f"全体：毛 {test['ret'].mean() * 100:+.3f}%、净 {(test['ret'].mean() - COST) * 100:+.3f}%、"
          f"胜率 {(test['ret'] > 0).mean() * 100:.1f}%、持有中位 {test['days'].median():.0f} 天、"
          f"均值 {test['days'].mean():.2f} 天")
    for reason, tag in (("up", "涨到就卖"), ("stop", "跌 5% 止损"), ("expired", "满 10 天到期")):
        g = test[test["reason"] == reason]
        if len(g):
            print(f"  {tag}：占 {(len(g) / len(test)) * 100:5.1f}%  "
                  f"{len(g):>6} 笔  毛均值 {g['ret'].mean() * 100:+.3f}%  "
                  f"中位 {g['ret'].median() * 100:+.3f}%  持有中位 {g['days'].median():.0f} 天")

    print("\n=== 按信号日涨幅分桶（同一条规则，人群细分）===")
    for lo, hi, name in BUCKETS:
        g = test[(test["signal_chg"] >= lo) & (test["signal_chg"] < hi)]
        if len(g) < 30:
            print(f"  {name:<22} {len(g):>6} 笔  样本太薄，不给读数")
            continue
        print(f"  {name:<22} {len(g):>6} 笔  净均值 {(g['ret'].mean() - COST) * 100:+.3f}%  "
              f"胜率 {(g['ret'] > 0).mean() * 100:4.1f}%  止损率 "
              f"{(g['reason'] == 'stop').mean() * 100:4.1f}%")

    pred = pd.read_parquet(REPO / "data/qlib" / args.score)
    pred["datetime"] = pd.to_datetime(pred["datetime"])
    scored = test.merge(pred.rename(columns={"score": "model_score"}),
                       left_on=["day", "instrument"], right_on=["datetime", "instrument"])
    print(f"\n=== 有分数 vs 随机（安慰剂）·每天取 5 笔，{len(scored)} 笔可比 ===")
    base = scored.groupby("day")["ret"].mean()
    rng = np.random.default_rng(42)
    for n in (3, 5, 10):
        head = scored.sort_values(["day", "model_score"], ascending=[True, False]) \
                    .groupby("day", sort=False).head(n)
        pp_m, t_m = paired(head, scored)
        placebo = []
        for _ in range(200):
            pick = scored.groupby("day", sort=False).apply(
                lambda g: g.sample(min(n, len(g)), random_state=int(rng.integers(1 << 31))),
                include_groups=False)
            placebo.append(paired(pick, scored)[0])
        print(f"  头 {n:>2}：按模型分 净 {head['ret'].mean() * 100 - COST * 100:+.3f}% "
              f"配对 {pp_m:+.3f}pp t {t_m:+.2f} ｜ 随机 配对均值 {np.mean(placebo):+.3f}pp "
              f"（200 次重抽，±{2 * np.std(placebo):.3f}pp 内算噪声）")
    ev.to_parquet("/tmp/rule_events.parquet")
    print("\n[事件明细] /tmp/rule_events.parquet（一次性产物，不是资产）")


if __name__ == "__main__":
    main()
