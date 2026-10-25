"""跑一遍「热门偏好榜」（hot_pref）并打印：只读，不写库、不发消息、不碰线上进程。

用途：改判据/改阈值之后先在这里看名单，确认无误再让 8003 那条链认账
（线上生效要 `sudo supervisorctl restart selectstock-plugin-qlib`，那一步由用户跑）。

    python3 scripts/qlib_pref_check.py [--day 2026-10-22] [--top 30] [--min-mv 50]

口径与滞后都打在表头：板块热度/新闻利好取的是哪一天、三道闸各挡了多少只。
冷打分要几百秒（全池 2,800+ 只），这是它的固有成本，不是卡住了。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_selector_qlib import artifacts, pref, serving  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=None, help="打分日，默认取面板最后一天")
    ap.add_argument("--top", type=int, default=pref.DEFAULT_TOPK)
    ap.add_argument("--min-mv", type=float, default=pref.MIN_FLOAT_MV, help="流通市值下限（亿元）")
    ap.add_argument("--json", action="store_true", help="把名单打成 JSON（给台账/对比用）")
    args = ap.parse_args()

    art = artifacts.load()
    # 只读试跑不发钉钉：覆盖率告警是线上那条链该说的话，这里说一遍就是重复打扰。
    serving._send_alert = lambda title, text: print(f"[只读试跑，告警不外发] {title}")
    print(f"模型 {art.run_id}（训练止于 {art.meta.get('train_end')}），票池来源 {serving.POOL_SOURCE}")
    rows = serving.day_ranking()
    if not rows:
        print("票池里没有可打分的股票（日线断档或票池过期）")
        return 1
    day = args.day or rows[0]["date"]
    if day != rows[0]["date"]:
        print(f"注意：指定了 {day}，但打分结果是 {rows[0]['date']} 的——这条只做当日榜，不补历史")
        return 1
    res = pref.build_from_rows(rows, day=day, top=args.top, min_float_mv=args.min_mv)
    names = serving.stock_names([r["code"] for r in res.rows]) if res.rows else {}
    for r in res.rows:
        r["name"] = names.get(r["code"], r.get("name") or "")

    s = res.stats
    print(f"\n== 筛数（{day}）==")
    print(f"模型榜进票池 {s['in']} 只 → 热门板块候选 {s['candidates']} 只 → 出榜 {s['out']} 只")
    print(f"闸门：无热门板块 {s['no_hot_board']}、小市值 {s['small_cap']}、市值缺数 {s['mv_missing']}、"
          f"趋势向下 {s['trend_down']}、趋势判不了 {s['trend_unknown']}；当日有新闻利好的 {s['news_hit']} 只")
    print(f"阈值：流通市值 ≥ {s['min_float_mv']:.0f} 亿、热门板块取前 {s['hot_boards']} 个")
    print(f"数据时点：板块热度 {s['heat_day']}（滞后 {s['heat_lag_days']} 天）、"
          f"新闻 {s['news_day']}（滞后 {s['news_lag_days']} 天）、市值 {s['mv_day']}")
    print(f"利好覆盖：板块 {s['news_boards']} 个、个股 {s['news_stocks']} 只")

    if args.json:
        print(json.dumps({"stats": s, "rows": res.rows}, ensure_ascii=False, indent=2))
        return 0
    print(f"\n== 名单 前 {len(res.rows)} 只 ==")
    for r in res.rows:
        ex = r["extra"]
        print(f"{r['rank']:>3}. {r['name']}({r['code']}) 分 {r['score'] * 100:.1f} "
              f"板块「{ex['board_name']}」热度第 {ex['board_heat_rank']} "
              f"利好 +{ex['news_score']} 市值 {ex['float_mv']:.0f} 亿 "
              f"站上MA20 {ex['trend_strength'] * pref.TREND_STRENGTH_SPAN * 100:.1f}% "
              f"模型榜第 {ex['model_rank']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
