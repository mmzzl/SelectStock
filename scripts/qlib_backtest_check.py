"""qlib 回测引擎的手测脚本：直调内核，打印摘要形状与峰值内存，不碰 Celery、不写 Mongo。

用法（默认用 data/qlib/model 的正式产物）：
    python3 -u scripts/qlib_backtest_check.py --start 2025-09-01 --end 2026-09-01
冒烟产物跑法：
    QLIB_MODEL_DIR=data/qlib/model_smoke python3 -u scripts/qlib_backtest_check.py \
        --start 2021-07-01 --end 2021-12-31 --topk 20 --n-drop 3

看三件事：
1. 摘要形状是否和自研内核一致（metrics/equity/trades/executability 的键）；
2. 成交明细是不是真的有内容（trade_count>0、卖单带 pnl）；
3. 峰值 RSS 是否留在 celery worker 的 3 G cgroup 之下。
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
BACKEND = REPO / "stock_selector_api" / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from stock_selector_qlib import artifacts  # noqa: E402
from stock_selector_qlib.backtest import run_qlib_backtest  # noqa: E402

# 后端 _summary 会取的键，缺一个就是「页面少一栏」
NEEDED_METRIC_KEYS = ("total_return", "annual_return", "max_drawdown", "win_rate", "sharpe",
                      "payoff_ratio", "trade_count")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--start", default="2025-09-01")
    ap.add_argument("--end", default="2026-09-01")
    ap.add_argument("--capital", type=float, default=100000.0)
    ap.add_argument("--topk", type=int, default=int(os.environ.get("QLIB_TOPK", "30")))
    ap.add_argument("--n-drop", type=int, default=5)
    ap.add_argument("--benchmark", default="SH510300")
    args = ap.parse_args()

    art = artifacts.load()
    print(f"模型产物：{art.describe()}")

    stages = []
    params = {"start": args.start, "end": args.end, "capital": args.capital, "engine": "qlib",
              "topk": args.topk, "n_drop": args.n_drop, "benchmark": args.benchmark}
    result = run_qlib_backtest(SimpleNamespace(**params),
                               progress_cb=lambda pct, stage: stages.append((pct, stage)))

    metrics = result.metrics
    trades = result.trades
    sells = [t for t in trades if t["side"] == "sell"]
    keys_ok = all(k in metrics for k in NEEDED_METRIC_KEYS)
    trade_shape = all(set(t) == {"code", "name", "side", "date", "price", "shares", "amount",
                                 "fee", "pnl"} for t in trades[:5])
    print(f"窗口 {result.start_date}→{result.end_date}（请求结束日 {params['end']}）")
    print(f"metrics 键齐：{keys_ok}")
    print(json.dumps(metrics, indent=2, ensure_ascii=False))
    print(f"equity 点数：{len(result.equity)} 首末：{result.equity[:1]} … {result.equity[-1:]}")
    # 净值曲线必须自己讲得通：起点=初始资金，终点=metrics 里的 end_value
    first, last = result.equity[0]["total_value"], result.equity[-1]["total_value"]
    curve_ok = abs(first - args.capital) < 1.0 and abs(last - metrics["end_value"]) < 1.0
    print(f"净值曲线自洽（起点 {first:.0f} vs 本金 {args.capital:.0f}；"
          f"终点 {last:.0f} vs end_value {metrics['end_value']:.0f}）：{curve_ok}")
    # 有换手就一定有手续费，净值必然低于毛利；两者相等说明成本没进净值（qlib 的 return 列是毛收益）
    gross = metrics["gross_return"]
    caliber_ok = (not metrics["cost_annualized"]) or gross > metrics["total_return"]
    print(f"毛/净口径分离：毛利 {gross:.2%} > 净值 {metrics['total_return']:.2%}"
          f"（窗口内年化成本 {(metrics['cost_annualized'] or 0):.2%}）：{caliber_ok}")
    print(f"成交：{len(trades)} 笔（卖 {len(sells)} 笔，带 pnl 的 {sum(1 for t in sells if t['pnl'] is not None)} 笔）"
          f"，形状一致：{trade_shape}")
    for t in trades[-3:]:
        print(f"  {t['date']} {t['side']} {t['code']} {t['shares']:.0f} 股 @{t['price']:.2f} "
              f"额 {t['amount']:.0f} 费 {t['fee']:.1f} 盈亏 {t['pnl']}")
    print(f"executability：{json.dumps(result.executability, ensure_ascii=False)}")
    print(f"进度阶段：{stages}")
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    print(f"峰值 RSS：{peak:.0f} MB（celery worker 的 cgroup 上限是 3 G）")

    # 每一项都要进 ok，只打印不判等于没验收
    ok = (keys_ok and trade_shape and curve_ok and caliber_ok and len(trades) > 0
          and len(result.equity) > 1 and peak < 3000)
    print("RESULT " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
