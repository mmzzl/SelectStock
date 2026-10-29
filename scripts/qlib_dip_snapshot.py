"""用指定臂对某一天重新打分，直接回答「002636 在新模型里排第几、头 30 有几个板票」。

口径：打分池 = 线上 universe.txt（剔 300/301/688/689，与 8003 服务同源）；asof 默认 2026-10-28。
「板票」= asof 当天收盘相对昨收 ≥ +9.75%。

    python3 scripts/qlib_dip_snapshot.py data/qlib/model_batchG_w900 data/qlib/model_batchW_c3
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import pandas as pd  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("models", nargs="+", help="artifact 目录，如 data/qlib/model_batchG_w900")
    ap.add_argument("--asof", default="2026-10-28")
    ap.add_argument("--watch", default="SZ002636")
    ap.add_argument("--topk", type=int, default=30)
    ap.add_argument("--pool-file", default="",
                    help="改用 qlib instruments 池文件（如 liq900.txt），默认用线上 universe.txt 口径")
    args = ap.parse_args()

    from stock_selector_qlib import artifacts, serving

    serving.init_qlib()
    if args.pool_file:
        pool = [l.split("\t")[0].strip().upper() for l in
                Path(QLIB_INSTR / args.pool_file).read_text().splitlines() if l.strip()]
    else:
        pool = serving.score_pool(artifacts.load())      # 线上同一份池子（universe + 板块剔）

    from qlib.data import D

    asof_ts = pd.Timestamp(args.asof)
    px = D.features(pool, ["$close"], start_time="2026-10-20", end_time=args.asof)["$close"].unstack("instrument")
    chg = (px / px.shift(1) - 1.0).loc[asof_ts].dropna()
    board = set(chg[chg >= 0.0975].index)
    print(f"[池] {len(pool)} 只，asof {args.asof} 板票 {len(board)} 只，观察票 {args.watch}")

    for m in args.models:
        art = artifacts.load(Path(m))
        pred, stats = serving.score_frame(art, beg=args.asof, end=args.asof, pool=list(pool))
        s = pred.loc[args.asof].sort_values(ascending=False)
        rank = {inst: i + 1 for i, inst in enumerate(s.index)}
        head = list(s.index[:args.topk])
        n_board = sum(1 for i in head if i in board)
        label = Path(m).name.replace("model_", "")
        if args.watch in rank:
            sc = float(s[args.watch])
            watch = f"第 {rank[args.watch]} 名 / {len(s)}，分数 {sc:+.5f}"
        else:
            watch = "未打分（薄流动/缺数被剔）"
        print(f"[{label}] run={art.run_id} 已打分 {stats['scored']}/{stats['pool']} "
              f"(薄行 {stats['thin_rows']}，整票无行 {stats['no_row']})")
        print(f"  {args.watch}：{watch}，头 {args.topk} 里板票 {n_board} 只")
        print(f"  头 10：{[(i, round(float(s[i]), 4), '板' if i in board else '') for i in head[:10]]}")
        if board:
            inboard = {i: rank[i] for i in board if i in rank}
            top5 = sorted(inboard.items(), key=lambda kv: kv[1])[:5]
            print(f"  板票最好名次：{top5}")

        # 事件臂的子榜：只在「今日涨 ≥+0.1%」里排（它的训练行就是这个人群）
        up = chg[chg >= 0.001].index.intersection(s.index)
        if len(up) != len(s):
            sub = s[up].sort_values(ascending=False)
            sub_rank = {inst: i + 1 for i, inst in enumerate(sub.index)}
            wsub = sub_rank.get(args.watch)
            print(f"  [子榜 今日涨≥0.1%：{len(sub)} 只] 头 10："
                  f"{[(i, round(float(sub[i]), 4), '板' if i in board else '') for i in sub.index[:10]]}")
            print(f"  {args.watch} 子榜第 {wsub} 名" if wsub else f"  {args.watch} 不在子榜人群内")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
