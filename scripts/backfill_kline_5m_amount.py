#!/usr/bin/env python3
"""回补 stock_kline_5m 的 amount：全表（2026-07-06 起）成交额一直是 0——腾讯 m5 的
`mkline` 只有 6 列（§38 记过），`newfqkline` 不收 m5（param error，2026-10-28 实测），
分钟成交额本来就没人给过：不是 16 天断链才缺，是整张表都缺（14,046,260 行全为 0）。

源（唯一可得的一台）：东财 `push2his .../api/qt/stock/kline/get?klt=5&fqt=0`，
fields2 顺子 f51=时间 f52..f55=OHLC f56=量 f57=额（元）。

为什么先种 Cookie 再请求（refresh_em_cookie.py 头部 2026-10-20 实测）：
东财的 Cookie（qgqp_b_id/st_si…）只由页面 JS 生成，HTTP 响应 0 个 Set-Cookie，
纯 curl_cffi 拿不到；同一时间窗裸指纹 7/15 对带 Cookie 14/15 ⇒ **开跑先种、后才发请求**。
本机到 push2his 会被随机 RST，硬窗口按 IP 计、六个镜像一起断（fetch_fund_flow 注释）
⇒ 串行 + 复用连接 + 连败限流 + 失败如实报 pending，不硬撞。

口径（照抄 scripts/backfill_kline_amount.py 的规矩适配 5m）：
- 只写 amount，不改价格/量；库里这根 K 已非 0 就跳过（update 过滤 amount=0.0，天然幂等）；
- 每码判量纲：median(amount/(volume×close)) 贴 1 ⇒ 库里量是「手」；贴 0.01 ⇒ 量是「股」
  （688 字头那个坑，日线 backfill 已实测）——两个带都收，成交额是交易所原值、与量单位无关；
  都不贴（如额被 ×1e4）判量纲认不出，该码整码告警不写入；
- EM 对 klt=5 的服务端上限 lmt≈800（≈16 个交易日），更早的 62 天一次拿不到——
  覆盖度自检会如实报「哪些日子还是 0」，不把拿不到说成补完了。

用法:
    python scripts/backfill_kline_5m_amount.py --limit 5   # 联调：前 5 只
    python scripts/backfill_kline_5m_amount.py             # 全表逐支
    python scripts/backfill_kline_5m_amount.py --no-cookie # 复用现有 em_cookie.txt（调试）
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

import pymongo
from pymongo import UpdateOne

ROOT = Path(__file__).resolve().parents[1]

DB_NAME = "eastmoney_news"
COLL_NAME = "stock_kline_5m"
MONGO = ("127.0.0.1", 27017)
COOKIE_PATH = ROOT / "data" / "fund_flow" / "em_cookie.txt"
REFERER = "https://data.eastmoney.com/zjlx/detail.html"
URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
FIELDS1 = "f1,f2,f3,f4,f5,f6"
FIELDS2 = "f51,f52,f53,f54,f55,f56,f57"     # f51 时间 f52..55 OHLC f56 量 f57 额(元)

LMT = 800                    # 请求值不重要：服务端实际给 1536 根（≈32 个交易日，2026-10-28 实测）
                             # ⇒ 一支一请求盖得住 09-11 之后的 32 天；更早的 07-06~09-10 老端
                             # 这轮拿不到，覆盖度自检会如实报出来
RETRIES = 6                  # RST 是间歇的，单支多试几次
BREAKER_N = 20               # 连败 N 支 = 硬窗口开场，歇一轮再发（fetch_fund_flow 同款）
BREAKER_SLEEP = 60.0
SANITY_LO, SANITY_HI = 0.5, 2.0          # 隐含价/收盘价 ≈1：库量是「手」
SHARES_LO, SHARES_HI = 0.005, 0.02        # ≈0.01：库量是「股」（688 口径坑）


def seed_cookie() -> None:
    """先要 Cookie 后才有请求（跑 headless Chrome 过东财页面）
    `import refresh_em_cookie` 与 fetch_fund_flow 同招：`python3 scripts/xxx.py` 会把
    scripts/ 放 sys.path[0]，不需要包。写盘走 tmp+rename（读的一半别看成半截 Cookie）。"""
    cookie = importlib.import_module("refresh_em_cookie").seed_cookie_string()
    COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = COOKIE_PATH.with_suffix(".tmp")
    tmp.write_text(cookie, encoding="utf-8")
    tmp.replace(COOKIE_PATH)


def http_session():
    """curl_cffi Chrome 指纹 + 复用连接；Referer 对准 Cookie 的出生页，否则像「有身份没来历」。"""
    from curl_cffi import requests as cffi_requests
    s = cffi_requests.Session(impersonate="chrome")
    s.headers.update({"Referer": REFERER})
    if COOKIE_PATH.exists():
        s.headers["Cookie"] = COOKIE_PATH.read_text(encoding="utf-8").strip()
    return s


def secid_of(code: str) -> str:
    """东财 secid：沪=1、深=0；北交所按 0 试（库里出现 4/8 字头才会走到）。"""
    return ("1." if code.startswith(("6", "5", "9")) else "0.") + code


def parse_payload(text: str) -> tuple[str, list[tuple[str, float, float, float]]]:
    """(代码, 每根 (date 'YYYY-MM-DD HH:MM', amount, volume, close))；EM 时间带秒，库里只到分。"""
    node = json.loads(text).get("data")
    if node is None:
        return "", []                       # secid 查无此票（如北交所不在东财覆盖）
    bars: list[tuple[str, float, float, float]] = []
    for k in node.get("klines") or []:
        c = k.split(",")
        bars.append((c[0][:16], float(c[6]), float(c[5]), float(c[2])))
    return node.get("code") or "", bars


def scale_ok(bars) -> str | None:
    """量纲判定：用东财自带收盘当参照，median(amount/(vol×close)) 落两个带之一。
    返回 'yuan' | 'shares' | None（认不出 ⇒ 整码不写，只告警）。"""
    ratios = sorted(amt / (vol * close) for _, amt, vol, close in bars if vol > 0 and close > 0)
    if not ratios:
        return None
    med = ratios[len(ratios) // 2]
    if SANITY_LO <= med <= SANITY_HI:
        return "yuan"
    if SHARES_LO <= med <= SHARES_HI:
        return "shares"
    return None


def fetch_amount(sess, code: str) -> list[tuple[str, float, float, float]]:
    last = None
    for i in range(RETRIES):
        try:
            r = sess.get(URL, params={"secid": secid_of(code), "klt": 5, "fqt": 0,
                                      "beg": 0, "end": 20500101, "lmt": LMT,
                                      "fields1": FIELDS1, "fields2": FIELDS2}, timeout=15)
            text = r.text
            _code, bars = parse_payload(text)
            if not bars:
                raise RuntimeError(f"无 klines（resp head: {text[:80]}）")
            return bars
        except Exception as e:
            last, _ = e, time.sleep(min(1.0 * (i + 1), 6.0))
    raise RuntimeError(f"{code}: {last}")


def write_amount(coll, code: str, bars) -> int:
    """只 fill 空额：date 命中、amount 仍为 0 ⇒ $set。库里非 0 不碰、EM 给 0 不写、
    upsert 不开 ⇒ 库里没有的 bar 不造行。返回实际修改的行数。"""
    ops = [UpdateOne({"code": code, "date": d, "amount": 0.0}, {"$set": {"amount": amt}})
           for d, amt, _, _ in bars if amt > 0]
    if not ops:
        return 0
    res = coll.bulk_write(ops, ordered=False)
    return int(res.modified_count)


def main():
    ap = argparse.ArgumentParser(description="补 stock_kline_5m 成交额（东财 klt=5，先 Cookie 后请求）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只（0=全部，联调用）")
    ap.add_argument("--no-cookie", action="store_true", help="复用现有 em_cookie.txt（调试用）")
    args = ap.parse_args()

    if not args.no_cookie:
        print("[cookie] 按惯例每轮现种一份身份（节流按会话累计，旧档翻过页就馊）…")
        try:
            seed_cookie()
            print("[cookie] OK →", COOKIE_PATH)
        except Exception as e:
            print(f"[cookie] 现种失败（{type(e).__name__}: {e}）"
                  f"{'；但有旧档，硬着头皮先试' if COOKIE_PATH.exists() else '；且无旧档 ⇒ 退出'}")
            if not COOKIE_PATH.exists():
                sys.exit(1)

    coll = pymongo.MongoClient(*MONGO)[DB_NAME][COLL_NAME]
    codes = sorted({r["_id"] for r in coll.aggregate([{"$group": {"_id": "$code"}}], allowDiskUse=True)})
    total_zero = coll.count_documents({"amount": 0.0})
    print(f"[口径] {len(codes)} 支 / 全表 amount==0 {total_zero} 行")

    sess = http_session()
    limit = args.limit or len(codes)
    t0 = time.time()
    written, failed, rejected = 0, [], 0
    breaker = 0
    for n, code in enumerate(codes[:limit], 1):
        try:
            bars = fetch_amount(sess, code)
        except Exception:
            failed.append(code)
            breaker += 1
            if breaker >= BREAKER_N:
                print(f"[硬窗口] 连败 {breaker} 支 ⇒ 歇 {BREAKER_SLEEP:.0f}s 再发（按 IP 计的闸，撞不动）")
                time.sleep(BREAKER_SLEEP)
                breaker = 0
            continue
        breaker = 0
        kind = scale_ok(bars)
        if kind is None:
            rejected += 1
            if bars:
                print(f"[量纲] {code} 认不出（{len(bars)} 根，暂不写）")
        else:
            written += write_amount(coll, code, bars)
        if n % 100 == 0:
            print(f"[{n}/{limit}] 已补 {written} 根 | 失败 {len(failed)} | 量纲拒 {rejected} | {time.time()-t0:.0f}s")
        time.sleep(0.15)
    print(f"[落库] {written} 根 | {limit - len(failed)}/{limit} 支成功 | 量纲拒 {rejected} | {time.time()-t0:.0f}s")
    if failed:
        print(f"[如实报] pending {len(failed)} 支（可重跑，不划算的部分不伪装 0）：{' '.join(failed[:30])}")

    print("[覆盖度] 逐日 amount==0 余量（拿不到的日子如实在此，别当成补完）：")
    for r in coll.aggregate([
            {"$match": {"amount": 0.0}},
            {"$group": {"_id": {"$substrCP": ["$date", 0, 10]}, "n": {"$sum": 1}}},
            {"$sort": {"_id": 1}}], allowDiskUse=True):
        print(f"  {r['_id']}  {r['n']}")


if __name__ == "__main__":
    main()
