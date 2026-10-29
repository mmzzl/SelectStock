"""按票拉东财**分红送配明细**入新集合 `stock_adj_factor`（A，docs §48）——不动 `stock_kline` 一根价格。

要解决的问题（§48.2）：面板与 `stock_kline` 那 1,227 万行的价格都是**裸价**（`factor` 恒 1；
`kline_spider.py` 里腾讯接口缺 `qfqday` 时静默回落成 `day`，却照样写 `adjust:"qfq"`，所以标错了）。
裸价上算收益，除权那天会凭空多出一截假跌——§45 的尺子 13,566 个事件有 229 个（1.69%）被碰到，
方向偏保守；−5% 止损线也会被假跳空误触。

为什么拉事件而不是拉厂商的复权价（§48.1 判决，2026-10-29）：东财 `fqt=2` 的后复权价不是比例复权——
600519 在 2015-01→07 的 120 天上拟合 `hfq = 4.5680×(裸价+19.5942)` 到最大残差 0.0052 元，
乘法假设的最大相对残差却是 2.701%，也就是它的日涨幅被系统性压缩一成；腾讯 hfq 更糟（600608 给负价、
000018 日间因子抖 43%）。因子必须由除权除息事件按交易所公式推，算式在 `stock_selector/adjfactor.py`。

入库只存**台阶**（一票一个除权日一行：前收盘、派息/送/转、台阶比值、累计因子、校验残差、suspect），
不存 12M 行复权价：复权价 = 裸价 × 因子，本地算就行，存两份迟早对不上。
`stock_adj_factor_run` 记每票结果 ⇒ 断点续跑；`--since` 支持增量（新的年度分红季过后补抓一轮）。

还带一道完备性自检：`unexplained_gaps` 找「跌幅超过涨跌停、日历里却查不到事件」的那天——
这是判断事件表抓全了没有的唯一办法（配股不在这张报表里，真漏了就会在这里冒头）。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import pymongo
import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_selector.adjfactor import (build_factors, parse_events,  # noqa: E402
                                      unexplained_gaps)

URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REPORT = "RPT_SHAREBONUS_DET"
PAGE_SIZE = 500              # 一票全部历史一屏装得下（600519 十一年 25 行）
COL_FACTOR = "stock_adj_factor"
COL_RUN = "stock_adj_factor_run"
COL_KLINE = "stock_kline"
FREQUENCIES = (0, 9)         # 两批互补的日线，必须并起来读
REQUEST_TIMEOUT = 15
RETRIES = 3
BACKOFF = 1.5
WINDOW_SLEEP = 120.0
WINDOW_FAILS = 10
SLEEP = 0.2

GEM_PREFIX = ("30", "68")    # 创业板 / 科创板 ±20%
BJ_PREFIX = ("4", "8", "9")  # 北交所 ±30%

logger = logging.getLogger("kline_factor")
_CATCH: tuple = (requests.RequestException, ValueError)
_SESSION = None


def session():
    """进程内唯一 GET 通道；curl_cffi（Chrome 指纹）优先，实测它能过普通 requests 被掐的窗。"""
    global _SESSION, _CATCH
    if _SESSION is None:
        try:
            from curl_cffi import requests as cffi_requests
            _SESSION = cffi_requests.Session(impersonate="chrome")
            _CATCH = (cffi_requests.exceptions.RequestException, ValueError)
        except Exception:
            _SESSION = requests.Session()
            _SESSION.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"})
        _SESSION.headers.update({"Referer": "https://data.eastmoney.com/gphx/"})
    return _SESSION


def limit_pct(code: str) -> float:
    """按板块给涨跌停幅度：写死 10% 会把 ±20%/±30% 的正常波动全报成日历漏了。"""
    if code.startswith(GEM_PREFIX):
        return 0.20
    if code.startswith(BJ_PREFIX):
        return 0.30
    return 0.10


def factor_docs(code: str, steps, crawl_time: str) -> list[dict]:
    """台阶 → 一票一除权日的文档；_id 固定 `code|date` ⇒ 重跑只覆盖不堆重复行。"""
    return [{"_id": f"{code}|{s['date']}", "code": code, "date": s["date"],
             "prev_close": float(s["prev_close"]), "cash": float(s["cash"]),
             "bonus": float(s["bonus"]), "transfer": float(s["transfer"]),
             "step": float(s["step"]), "factor": float(s["factor"]),
             "residual": s["residual"], "suspect": bool(s["suspect"]),
             "crawl_time": crawl_time} for s in steps]


def pending(codes, done, since=None) -> list[str]:
    """还没成功抓过的票。`since` 给出时只认该时刻之后抓的（增量：新区间里可能出了新除权）。"""
    fresh = set()
    for d in done or []:
        if d.get("ok") and (since is None or str(d.get("done_at", "")) >= since):
            fresh.add(d["code"])
    return [c for c in codes if c not in fresh]


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def daily_bars(col, code: str) -> list[dict]:
    """我们自己的裸价日线（两批并集、按日升序、丢掉 close≤0 的占位行）。只读，不改。"""
    by_date: dict[str, dict] = {}
    for freq in FREQUENCIES:                   # 先 f=0 再用 f=9 覆盖，与 qlib_export 的取舍一致
        cur = col.find({"code": code, "frequency": freq},
                       {"date": 1, "open": 1, "high": 1, "low": 1, "close": 1, "_id": 0})
        for doc in cur.sort("date", 1):
            close = doc.get("close")
            if not close or float(close) <= 0:
                continue
            by_date[str(doc["date"])[:10]] = {
                "date": str(doc["date"])[:10], "open": float(doc.get("open") or close),
                "high": float(doc.get("high") or close), "low": float(doc.get("low") or close),
                "close": float(close)}
    return [by_date[d] for d in sorted(by_date)]


def fetch_events(code: str, sleeper=time.sleep):
    """拉一票的分红送配明细（翻页到 count 为止）。重试用尽返回 None＝这票本轮没抓到。"""
    rows, page = [], 1
    while True:
        params = {"reportName": REPORT, "columns": "ALL",
                  "filter": f'(SECURITY_CODE="{code}")', "pageNumber": str(page),
                  "pageSize": str(PAGE_SIZE), "sortColumns": "EX_DIVIDEND_DATE",
                  "sortTypes": "-1", "source": "WEB", "client": "WEB"}
        body = None
        for attempt in range(RETRIES + 1):
            try:
                resp = session().get(URL, params=params, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                body = resp.json()
                break
            except _CATCH:
                if attempt < RETRIES:
                    sleeper(min(BACKOFF * (2 ** attempt), 20.0))
        if body is None:
            return None
        result = body.get("result") or {}
        rows.extend(result.get("data") or [])
        count = int(result.get("count") or 0)
        if page * PAGE_SIZE >= count or not (result.get("data") or []):
            return rows
        page += 1


def run_code(kcol, code: str) -> dict:
    """一票的完整流程：本地裸价 + 事件表 → 因子台阶 → 完备性自检。失败只回 ok=False。"""
    raw = fetch_events(code)
    if raw is None:
        return {"code": code, "ok": False, "error": "throttled"}
    bars = daily_bars(kcol, code)
    if len(bars) < 2:
        return {"code": code, "ok": False, "error": f"bars={len(bars)}"}
    events = parse_events(raw, code=code)
    steps = build_factors(bars, events)
    gaps = unexplained_gaps(bars, steps, limit_pct=limit_pct(code))
    return {"code": code, "ok": True, "bars": len(bars), "first": bars[0]["date"],
            "last": bars[-1]["date"], "plan_rows": len(raw), "events": len(events),
            "steps": len(steps), "suspect_steps": sum(1 for s in steps if s["suspect"]),
            "gap_days": len(gaps),
            "gap_sample": [{"date": g["date"], "raw_ret": round(g["raw_ret"], 4)} for g in gaps[:5]],
            "suspect_sample": [{"date": s["date"], "residual": round(s["residual"], 4)}
                               for s in steps if s["suspect"]][:5],
            "table": steps}


def attempt(db, code: str, *, dry_run: bool = False) -> dict:
    """一票的「抓+落库」，任何意外都折成 `ok=False`：一轮 6,900 多票不能挂在一只票上。

    第一次全量跑就断在 002043（同一天两笔分红写出重复 `_id` ⇒ BulkWriteError 直接打死进程，
    后面 6,300 多票没抓）。错误文本截 200 字符，BulkWriteError 会把整批文档回显出来。
    """
    try:
        res = run_code(db[COL_KLINE], code)
        if res["ok"] and not dry_run:
            write_code(db, res)
        return res
    except Exception as exc:  # noqa: BLE001 单票失败只记这一票
        return {"code": code, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}


def mongo_db():
    cfg_path = ROOT / "stock_selector" / "config" / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    m = (cfg or {}).get("mongodb") or {}
    client = pymongo.MongoClient(host=m.get("host", "127.0.0.1"),
                                 port=int(m.get("port", 27017)),
                                 serverSelectionTimeoutMS=8000)
    return client[m.get("database", "eastmoney_news")]


def universe(db) -> list[str]:
    """要抓的票：`stock_kline` 日线两批里出现过的全部 code（面板人口的真源）。"""
    return sorted(db[COL_KLINE].distinct("code", {"frequency": {"$in": list(FREQUENCIES)}}))


def write_code(db, res: dict) -> None:
    """整票替换：先删该票旧台阶再插新的，重跑不会留下过因子。"""
    fcol, rcol = db[COL_FACTOR], db[COL_RUN]
    stamp = now_str()
    fcol.delete_many({"code": res["code"]})
    docs = factor_docs(res["code"], res["table"], stamp)
    if docs:
        fcol.insert_many(docs, ordered=False)
    run = {k: v for k, v in res.items() if k != "table"}
    run["done_at"] = stamp
    rcol.update_one({"code": res["code"]}, {"$set": run}, upsert=True)


def report(db) -> int:
    """一次聚合读完，别分两次查——全量还在跑时 `count_documents` 与聚合之间会插进新票，
    于是出现「成功 5015 / 已抓 5014 ⇒ 失败 -1」这种看着像数据坏了的读数。"""
    agg = list(db[COL_RUN].aggregate([
        {"$group": {"_id": "$ok", "codes": {"$sum": 1}, "steps": {"$sum": "$steps"},
                    "suspect": {"$sum": "$suspect_steps"}, "gaps": {"$sum": "$gap_days"},
                    "events": {"$sum": "$events"}}},
        {"$sort": {"_id": -1}}]))
    ok = next((a for a in agg if a["_id"] is True), {})
    fail = sum(a["codes"] for a in agg if a["_id"] is not True)
    print(f"[factor] 已抓 {ok.get('codes', 0) + fail} 票：成功 {ok.get('codes', 0)}、失败 {fail}")
    print(f"  事件 {ok.get('events', 0)} 笔 → 台阶 {ok.get('steps', 0)} 段"
          f"（stock_adj_factor {db[COL_FACTOR].count_documents({})} 行）；"
          f"suspect {ok.get('suspect', 0)}、超涨跌停却查不到事件的天数 {ok.get('gaps', 0)}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--codes", help="逗号分隔白名单（冒烟用）")
    ap.add_argument("--limit", type=int, default=0, help="本轮最多抓几票（0=全量）")
    ap.add_argument("--since", help="只认该时刻之后的抓取记录，如 '2026-10-29 00:00:00'（增量）")
    ap.add_argument("--sleep", type=float, default=SLEEP)
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写 Mongo")
    ap.add_argument("--status", action="store_true", help="只报进度与自检数")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db = mongo_db()
    if args.status:
        return report(db)

    codes = args.codes.split(",") if args.codes else universe(db)
    done = list(db[COL_RUN].find({}, {"code": 1, "ok": 1, "done_at": 1, "_id": 0}))
    todo = pending(codes, done, since=args.since)
    if args.limit:
        todo = todo[: args.limit]
    print(f"[factor] 人口 {len(codes)} 票，待抓 {len(todo)} "
          f"{'DRY-RUN' if args.dry_run else '写入 ' + COL_FACTOR}")

    stats = {"ok": 0, "fail": 0, "steps": 0, "suspect": 0, "gaps": 0}
    fails = 0
    for i, code in enumerate(todo, 1):
        res = attempt(db, code, dry_run=args.dry_run)
        if res["ok"]:
            stats["ok"] += 1
            stats["steps"] += res["steps"]
            stats["suspect"] += res["suspect_steps"]
            stats["gaps"] += res["gap_days"]
            fails = 0
        else:
            stats["fail"] += 1
            fails += 1
            if not args.dry_run:
                db[COL_RUN].update_one(
                    {"code": code}, {"$set": {"code": code, "ok": False,
                                              "error": res["error"], "done_at": now_str()}},
                    upsert=True)
        if i % 20 == 0 or not res["ok"]:
            if res["ok"]:
                print(f"  [{i}/{len(todo)}] {code} bars={res['bars']} events={res['events']} "
                      f"steps={res['steps']} suspect={res['suspect_steps']} gaps={res['gap_days']} "
                      f"{res['gap_sample'][:2] if res['gap_days'] else ''}")
            else:
                print(f"  [{i}/{len(todo)}] {code} 失败 {res['error']}")
        if fails >= WINDOW_FAILS:
            print(f"  [factor] 连续 {fails} 票被掐，睡 {WINDOW_SLEEP:.0f}s 避开时间窗")
            time.sleep(WINDOW_SLEEP)
            fails = 0
        time.sleep(args.sleep)

    print("[factor] 本轮 " + json.dumps(stats, ensure_ascii=False))
    print("  口径：steps=按事件推出的因子台阶数；suspect=除权日实际跌幅与事件隐含跌幅差超 20% 的段；"
          "gaps=跌幅超涨跌停却查不到事件的天数（配股不在这张报表里，漏了会在这里冒头）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
