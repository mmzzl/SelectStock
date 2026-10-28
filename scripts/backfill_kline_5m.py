#!/usr/bin/env python3
"""补 stock_kline_5m 断链（§37.16 裁决：要做分钟线必须先补 2026-09-29 之后的洞）。

口径（写死，2026-10-28 首跑）：
- 源：`proxy.finance.qq.com/ifzqgtimg/.../mkline`（腾讯 m5，800 根/请求，正好覆盖 09-29→今 的缺口）。
  已对齐校验：与库内存量同类 K 逐位一致（OHLC 全等、成交量单位 ratio=1.0）。
  东财 push2his 本机间歇 RST（README 已记），腾讯 m5 走 web.ifzq 会 301→web3（本机 DNS 不解），
  换 proxy 域名即可。腾讯 m5 不给成交额 ⇒ amount 落 0.0，与存量同形。
- 范围：库里已有的代码（存量最新交易日的名单），**只补不扩**——新上市票不进本次范围。
- 起点：库里最大 date 的日期部分之后的第一个 bar（增量、可重跑；存量 bar 一字不动）。
- 覆盖度：如果未来断链超过 800 根（≈16 个交易日）本脚本拿不到更早的，会如实报行数偏少。

用法：python3 scripts/backfill_kline_5m.py                  # 补到最新
      python3 scripts/backfill_kline_5m.py --calibrate-only  # 只对齐校验，不写库
      python3 scripts/backfill_kline_5m.py --limit 20        # 只处理前 N 只（联调用）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime

import pymongo
from pymongo import UpdateOne

DEFAULT_MONGO = ("127.0.0.1", 27017)
DB_NAME = "eastmoney_news"
COLL_NAME = "stock_kline_5m"
URL = ("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/kline/mkline?param={sym},m5,,800")
BULK = 500


def symbol_of(code: str) -> str:
    # 沪市：60/68/9/5 开头；深市：0/1/2/3 开头；北交所 8/4 开头走 BJ 前缀（库里若没有此形状就不会用到）。
    if code.startswith(("6", "5", "9")):
        return f"sh{code}"
    if code.startswith(("4", "8")):
        return f"bj{code}"
    return f"sz{code}"


def fetch_bars(sess, code: str, retries: int = 6) -> list[tuple[str, float, float, float, float, float]]:
    """一支票的 m5 最近 800 根，返回 [(date, open, close, high, low, volume)]（date 已转 'YYYY-MM-DD HH:MM'）。"""
    last = None
    for i in range(retries):
        try:
            r = sess.get(URL.format(sym=symbol_of(code)), timeout=20)
            node = json.loads(r.text)["data"][symbol_of(code)]
            key = next(k for k in node if "m5" in k)
            out = []
            for b in node[key]:
                d = b[0]
                out.append((f"{d[:4]}-{d[4:6]}-{d[6:8]} {d[8:10]}:{d[10:12]}",
                            float(b[1]), float(b[2]), float(b[3]), float(b[4]), float(b[5])))
            return out
        except Exception as e:
            last, _ = e, time.sleep(min(1.5 * (i + 1), 8.0))
    raise RuntimeError(f"{code}: {last}")


def latest_day_and_codes(coll) -> tuple[str, list[str], dict]:
    """最近一个**完整覆盖**的交易日 + 该天的 code 名单 + 一根样本 bar（全表扫，不建/不依赖新索引）。

    完整覆盖 = 当天 bar 数 ≥150,000（约 3,500 支 × 48 根）。不能只取 $max(date)：增量/
    冒烟跑过一半的日子也可能有 bar（如只有 20 支的 2026-10-28），拿它当基点会把缺口悄悄吞掉。
    """
    day = None
    for r in coll.aggregate(
            [{"$group": {"_id": {"$substrCP": ["$date", 0, 10]}, "n": {"$sum": 1}}},
             {"$match": {"n": {"$gte": 150_000}}},
             {"$sort": {"_id": -1}}, {"$limit": 1}],
            allowDiskUse=True):
        day = r["_id"]
    if not day:
        raise RuntimeError("库里没有任何一天达到 15 万行的完整覆盖口径")
    codes = sorted(r["_id"] for r in coll.aggregate(
        [{"$match": {"date": {"$regex": f"^{day}"}}}, {"$group": {"_id": "$code"}}],
        allowDiskUse=True))
    maxdate = f"{day} 15:00"
    probe = None
    for code in codes[:5]:
        probe = coll.find_one({"code": code, "date": maxdate}, {"_id": 0})
        if probe:
            break
    return day, codes, probe


def write_bars(coll, code: str, bars, crawl_day: str, now: str) -> int:
    newbars = [b for b in bars if b[0] > f"{crawl_day} 23:59"]
    if not newbars:
        return 0
    docs = [UpdateOne(
        {"code": code, "date": b[0]},
        {"$set": {"date": b[0], "code": code,
                  "open": b[1], "close": b[2], "high": b[3], "low": b[4],
                  "volume": round(b[5], 2), "amount": 0.0, "crawl_time": now}},
        upsert=True) for b in newbars]
    coll.bulk_write(docs, ordered=False)
    return len(docs)


def main():
    ap = argparse.ArgumentParser(description="补 stock_kline_5m 断链（腾讯 m5 裸价）")
    ap.add_argument("--calibrate-only", action="store_true", help="只校验对齐，不写库")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只（0=全部，联调用）")
    args = ap.parse_args()

    coll = pymongo.MongoClient(*DEFAULT_MONGO)[DB_NAME][COLL_NAME]
    from curl_cffi import requests as cffi_requests
    sess = cffi_requests.Session(impersonate="chrome")

    crawl_day, codes, probe = latest_day_and_codes(coll)
    print(f"[名单] 存量最后一天 {crawl_day}，{len(codes)} 支")
    if probe is None:
        print("[校验] 库里读不到样本 bar ⇒ 先看 mongo 连通性")
        sys.exit(1)

    # 1) 对齐校验：存量 bar 与 m5 同根 K 比 OHLC + 量（对不上就不写库）。
    probe_bars = fetch_bars(sess, probe["code"])
    same = next((b for b in probe_bars if b[0] == probe["date"]), None)
    matched = same is not None and all(
        abs(same[j] - probe[f]) < 0.51  # 存量对 volume 做了取整，容差半手/半股
        for j, f in ((1, "open"), (2, "close"), (3, "high"), (4, "low"), (5, "volume")))
    print(f"[校验] {probe['code']} {probe['date']} 对齐 {'OK' if matched else 'FAIL'} | "
          f"m5 {same[:5] if same else None} / 库 {[probe[k] for k in ('date','open','close','high','low')]}")
    if not matched:
        print("[校验] 对不上 ⇒ 不写库（源换了或口径漂了，先人工看）")
        sys.exit(1)
    if args.calibrate_only:
        return

    # 2) 逐支拉数，攒片 bulk upsert；单支失败不静默，收尾统一重试一轮。
    limit = args.limit or len(codes)
    slack = f"{crawl_day} 23:59"
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")
    t0 = time.time()
    failed: list[str] = []
    written = 0
    pending: list[UpdateOne] = []

    def flush():
        nonlocal pending
        if pending:
            coll.bulk_write(pending, ordered=False)
            pending.clear()

    for n, code in enumerate(codes[:limit], 1):
        try:
            written += write_bars(coll, code, fetch_bars(sess, code), crawl_day, now)
        except Exception:
            failed.append(code)
        if len(pending) >= BULK:
            flush()
        if n % 200 == 0:
            print(f"[{n}/{limit}] 已写 {written} 根 | 失败 {len(failed)} | {time.time() - t0:.0f}s")
    flush()

    if failed:
        retry, still = list(failed), []
        failed.clear()
        print(f"[重试] 对 {len(retry)} 支失败码再来一轮")
        for code in retry:
            try:
                written += write_bars(coll, code, fetch_bars(sess, code, retries=8), crawl_day, now)
            except Exception:
                still.append(code)
        failed = still
    print(f"[落库] 新写/更新 {written} 根，{limit - len(failed)} / {limit} 支成功")
    if failed:
        print(f"[如实报] 未补上（pending，不伪造 0）：{' '.join(failed[:40])}{' …' if len(failed) > 40 else ''}")

    # 3) 逐日覆盖度自检：断链哪天没补齐必须看得见。
    print("[覆盖度] 逐日行数（存量約 18.1 万/日；偏少的日子要人工看）：")
    for r in coll.aggregate(
            [{"$match": {"date": {"$gt": slack}}},
             {"$group": {"_id": {"$substrCP": ["$date", 0, 10]}, "n": {"$sum": 1}}},
             {"$sort": {"_id": 1}}], allowDiskUse=True):
        print(f"  {r['_id']}  {r['n']}{'  ← 偏少' if r['n'] < 150_000 else ''}")


if __name__ == "__main__":
    main()
