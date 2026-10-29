"""定点修复「除权前一根被复权快照改写」的假摔（docs §48.5）。

病是怎么来的（48.5）：历史批量写的是腾讯 `qfqday`（前复权），而增量只重写队尾那根。
于是只有**紧邻除权日的那一根**会被按新基准改写：库里存的那根 = 真裸价 × step，
凭空多出一根「跌 30%/43%」的假摔；再下一根又回到裸价基准，所以整条序列看着还是裸价。

修法是拿**独立裸价源**（sina 日线，只给最近 250 根）逐根核对：库里 ≈ 上游 × step 且库里 ≠ 上游才算确证，
然后用 `$set` 把那一根的价格四列改回上游裸价；量/额不动（复权不改量）。
旧值全量落一份 JSONL，回滚就是照着那份文件把四个字段改回去。

默认 **dry-run 只出对照表**，`--apply` 才写库。写库的过滤条件带上旧收盘，
并发/重跑都不会把已经改好的那根再改一遍。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.kline_coverage_check import _sina_bars, mongo_db  # noqa: E402

COL_KLINE = "stock_kline"
COL_FACTOR = "stock_adj_factor"
FREQUENCIES = (0, 9)
PRICE_FIELDS = ("open", "high", "low", "close")
ROLLBACK_DIR = ROOT / "data" / "kline_seam_repair"

# sina 与库里都是「同一天同一根」时才敢改：库里那根确实跟上游不一样（差到 1% 以上），
# 而且比值在这次日期的除权台阶 3% 以内——实测被改写的那批比值与交易所公式差 0.7%~1.8%
# （源自己的除权参考价按四舍五入算），所以 step 是旁证、上游裸价才是权威。
STEP_REL_TOL = 0.03
MIN_DIFF = 0.01

logger = logging.getLogger("seam_repair")


def board_limit(code: str):
    """该板块的涨跌停幅度；判不了（基金/指数/别的字头）返回 None＝不加这道判据。"""
    if code.startswith(("60", "00")):
        return 0.10
    if code.startswith(("30", "68")):
        return 0.20
    if code.startswith(("8", "4")):
        return 0.30
    return None


def candidate(prev, prev2, step, code: str):
    """除权日的前一根是不是被乘过 step。两个条件都要：跌得物理上不可能 + step 解释得通。"""
    if not prev or not prev2:
        return None
    limit = board_limit(code)
    if limit is None or not step or step >= 1.0:
        return None
    base = prev2.get("close") or 0
    stored = prev.get("close") or 0
    if base <= 0 or stored <= 0:
        return None
    implied = stored / base - 1
    if implied > -limit:                      # 真实行情就能跌这么多，别动
        return None
    band = ((1 - limit) * step - 1, (1 + limit) * step - 1)
    if not (band[0] - 1e-9 <= implied <= band[1] + 1e-9):
        return None                           # 深跌幅对不上这次除权＝别的病
    return {"implied": round(implied, 5), "band": (round(band[0], 5), round(band[1], 5)),
            "code": code, "date": str(prev.get("date", ""))[:10], "step": round(step, 6)}


def confirm(stored_close, upstream_close, step):
    """库里那根 ≈ 上游裸价 × step ⇒ 确证是被复权快照改写的。返回 (是否确证, 比值)。"""
    if not upstream_close or upstream_close <= 0 or not stored_close:
        return False, None
    ratio = stored_close / upstream_close
    ok = abs(ratio - step) <= STEP_REL_TOL * max(step, 1e-9) and abs(ratio - 1) >= MIN_DIFF
    return ok, round(ratio, 6)


def patch(bar: dict) -> dict:
    """只改价格四列。"""
    return {k: bar[k] for k in PRICE_FIELDS if bar.get(k) is not None}


def by_date(rows):
    """上游行按日切键（有的带时刻、价是字符串）。取不到返回空表＝不能断定。"""
    out = {}
    for r in rows or []:
        day = str(r.get("day", ""))[:10]
        if not day:
            continue
        rec = {"date": day}
        for k in PRICE_FIELDS:
            try:
                rec[k] = float(r.get(k))
            except (TypeError, ValueError):
                rec[k] = None
        if rec["close"]:
            out[day] = rec
    return out


def day(x) -> str:
    return str(x or "")[:10]


def load_events(db, since: str) -> dict:
    """code → [(除权日, step)]，按除权日升序。只取 `--since` 之后的（sina 只给最近 250 根）。"""
    cur = db[COL_FACTOR].find({"date": {"$gte": since}, "step": {"$lt": 1.0}},
                              {"code": 1, "date": 1, "step": 1}).sort([("code", 1), ("date", 1)])
    ev: dict[str, list] = {}
    for d in cur:
        ev.setdefault(d["code"], []).append((day(d["date"]), d["step"]))
    return ev


def load_stored(db, code: str, since: str) -> list:
    cur = db[COL_KLINE].find({"code": code, "frequency": {"$in": list(FREQUENCIES)},
                              "date": {"$gte": f"{since} 00:00"}}).sort("date", 1)
    return list(cur)


def find_candidates(db, since: str) -> list:
    """第一遍只看库内 + 事件日历，找出「嫌疑根」；第二遍才去问上游（嫌疑票才花请求）。"""
    ev = load_events(db, since)
    out = []
    for code, events in ev.items():
        bars = load_stored(db, code, since)
        by_day = {day(b["date"]): b for b in bars}
        dates = sorted(by_day)
        for ex_date, step in events:
            prev_days = [d for d in dates if d < ex_date]
            if len(prev_days) < 2:
                continue
            prev, prev2 = by_day[prev_days[-1]], by_day[prev_days[-2]]
            hit = candidate(prev, prev2, step, code)
            if hit:
                hit.update({"ex_date": ex_date, "frequency": prev.get("frequency"),
                            "stored_date": prev["date"], "stored": patch(prev),
                            "stored_id": prev.get("_id"),
                            "stored_close": prev.get("close")})
                out.append(hit)
    return out


def verify(db, hits: list) -> tuple[list, list]:
    """对嫌疑票逐只问一次上游裸价，分成「确证」与「核不到/本来就是裸价」。"""
    confirmed, unclear = [], []
    for code in {h["code"] for h in hits}:
        try:
            raw = by_date(_sina_bars(code))
        except Exception as exc:  # noqa: BLE001 上游问不到就退回「不能断定」，别猜
            logger.warning("sina %s: %s", code, exc)
            raw = {}
        for h in [x for x in hits if x["code"] == code]:
            up = raw.get(h["date"])
            if not up:
                h["reason"] = "上游没这根"
                unclear.append(h)
                continue
            ok, ratio = confirm(h["stored_close"], up["close"], h["step"])
            h["upstream"] = patch(up)
            h["upstream_close"] = up["close"]
            h["ratio"] = ratio
            if ok:
                confirmed.append(h)
            else:
                h["reason"] = "库里 ≈ 上游（本来就是裸价）" if ratio and abs(ratio - 1) < MIN_DIFF \
                    else "比值对不上 step"
                unclear.append(h)
    return confirmed, unclear


def apply_fixes(db, confirmed: list, report_path: Path) -> int:
    col = db[COL_KLINE]
    done = 0
    with report_path.open("a", encoding="utf-8") as fh:
        for h in confirmed:
            filt = ({"_id": h["stored_id"]} if h.get("stored_id") else
                    {"code": h["code"], "date": h["stored_date"],
                     "frequency": h["frequency"], "close": h["stored_close"]})
            res = col.update_one(filt, {"$set": patch(h["upstream"])})
            fh.write(json.dumps({"code": h["code"], "date": h["stored_date"],
                                 "ex_date": h["ex_date"], "step": h["step"],
                                 "old": h["stored"], "new": h["upstream"],
                                 "matched": res.matched_count}, ensure_ascii=False) + "\n")
            done += res.modified_count
            if not res.matched_count:
                logger.warning("%s %s 没匹配到（已被改过？）", h["code"], h["stored_date"])
    return done


def summary(confirmed, unclear) -> str:
    deep = [h for h in confirmed if h["implied"] <= -0.10]
    return (f"确证 {len(confirmed)} 根（其中隐含跌幅 ≥10% 的 {len(deep)} 根）、"
            f"核不到或对不上 {len(unclear)} 根")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", default="2025-08-01",
                    help="只看这段之后的除权事件（sina 只给最近 250 根日线，再早核不了）")
    ap.add_argument("--apply", action="store_true", help="真写 stock_kline（默认只出对照表）")
    ap.add_argument("--report", default=None, help="对照表/回滚文件落地路径")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db = mongo_db()
    hits = find_candidates(db, args.since)
    print(f"[seam] 窗口 {args.since}… 嫌疑根 {len(hits)} 根（票 {len({h['code'] for h in hits})} 只）")
    confirmed, unclear = verify(db, hits)
    print(f"  {summary(confirmed, unclear)}")
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    path = Path(args.report) if args.report else ROLLBACK_DIR / f"{stamp}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for h in sorted(confirmed + unclear, key=lambda x: (x["code"], x["date"])):
            fh.write(json.dumps(h, ensure_ascii=False, default=str) + "\n")
    for h in sorted(confirmed, key=lambda x: x["implied"])[:15]:
        print(f"    {h['code']} {h['date']} 库 {h['stored']['close']} → 上游 {h['upstream_close']} "
              f"(step {h['step']:.4f}, 隐含 {h['implied'] * 100:+.1f}%)")
    print(f"  对照表：{path}")
    if args.apply:
        n = apply_fixes(db, confirmed, path.with_suffix(".applied.jsonl"))
        print(f"  已改 {n} 根（旧值在同一目录的 .applied.jsonl，回滚照它 $set 回去）")
    else:
        print("  DRY-RUN：没动库（要改加 --apply）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
