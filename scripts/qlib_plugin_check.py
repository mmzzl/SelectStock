"""qlib 插件的验收脚本：把「换插件后分数还是同一个模型给的」这条门槛跑成可复核的读数。

三条检查（预注册在 stock_selector/docs/qlib路线数据口径与验收门槛.md §9）：
1. **等价性**：插件给最近一天的前 30 只，与训练脚本 `scripts/qlib_run.py` 落盘的
   `data/qlib/selection_<date>.parquet` 必须同一批票、同一顺序、原始分逐只相等（≤1e-6）。
   这条过了才说明「短窗 + 存下来的 mean/std」复刻的是同一个模型，而不是像另一个模型。
2. **契约**：插件的 /api/selector/run 能被后端的取键方式吃下（code/name/signal/reason/…/extra）。
3. **代价**：进程峰值 RSS 与单次打分耗时——服务进程不该驻 3.4 GB（那是训练全跑的量级），
   否则 celery worker 的 3 G cgroup 会把它打死。

用法：python3 -u scripts/qlib_plugin_check.py [--date 2026-10-16] [--topk 30]
"""
from __future__ import annotations

import argparse
import json
import resource
import sys
import time
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from stock_selector_qlib import artifacts, serving  # noqa: E402

TOL = 1e-6


def peak_mb() -> float:
    """ru_maxrss 单位是 KB；子进程也算进来（RUSAGE_SELF 只算本进程）。"""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", default="", help="比对日，缺省用 qlib 日历最后一天")
    ap.add_argument("--topk", type=int, default=30)
    ap.add_argument("--model-dir", default="")
    args = ap.parse_args()

    art = artifacts.load(Path(args.model_dir)) if args.model_dir else artifacts.load()
    print(f"模型产物：{art.describe()}")

    started = time.time()
    serving.init_qlib()
    rows = serving.latest_scores(topk=args.topk, refresh=True)
    took = time.time() - started
    date = args.date or rows[0]["date"]

    ref_path = REPO / "data" / "qlib" / f"selection_{date}.parquet"
    if not ref_path.exists():
        print(f"FAIL 参照文件不存在：{ref_path}（先跑 scripts/qlib_run.py 出名单）")
        return 1
    ref = pd.read_parquet(ref_path)
    got = pd.DataFrame(rows)

    same_set = set(got["code"]) == set(ref["code"])
    same_order = list(got["code"]) == list(ref["code"].astype(str))
    # 训练脚本落盘列名叫 score，插件落盘列名叫 score_raw（score 让给了 0~1 分位）——
    # 两边比的是同一个东西：模型的原始输出
    ref_map = dict(zip(ref["code"].astype(str), ref["score"].astype(float)))
    pairs = [(str(c), float(r), ref_map[str(c)]) for c, r in zip(got["code"], got["score_raw"])
             if str(c) in ref_map]
    max_abs = max((abs(raw - train) for _, raw, train in pairs), default=None)

    print(f"比对日 {date}：插件 {len(got)} 只 / 训练脚本 {len(ref)} 只")
    print(f"  同一批票：{same_set}  同一顺序：{same_order}  可比对：{len(pairs)} 只")
    print(f"  原始分最大差：{max_abs}（容差 {TOL}）")
    print(f"  打分耗时：{took:.1f} 秒  进程峰值 RSS：{peak_mb():.0f} MB")

    from fastapi.testclient import TestClient  # 契约检查用进程内 client，不起端口、不碰 8002

    from stock_selector_qlib import api

    client = TestClient(api.app)
    health = client.get("/api/health").json()
    body = client.post("/api/selector/run", json={"strategies": None}).json()
    items = body.get("data") or []
    keys = sorted(items[0].keys()) if items else []
    needed = ["asof", "code", "extra", "name", "price", "rank", "reason", "score", "signal", "strategy"]
    print(f"  /api/health：{health.get('status')}  名单条数：{len(items)}")
    print(f"  契约键齐：{all(k in keys for k in needed)}（缺 {sorted(set(needed) - set(keys))}）")
    for item in items[:2]:
        print(f"    {item['code']} {item['name']} rank={item['rank']} price={item['price']} "
              f"score={item['score']} extra.score_raw={item['extra'].get('score_raw')}")

    ok = same_set and same_order and max_abs is not None and max_abs <= TOL \
        and all(k in keys for k in needed) and len(items) > 0
    print("RESULT " + ("PASS" if ok else "FAIL"))
    if not ok:
        print("  口径不通过就意味着插件给的不是同一个模型的分数，不能替换 8002")
    print(json.dumps({"date": date, "equal_set": same_set, "equal_order": same_order,
                      "max_abs_score_diff": max_abs, "seconds": round(took, 1),
                      "peak_mb": round(peak_mb(), 1)}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
