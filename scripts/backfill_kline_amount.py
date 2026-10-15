"""回补断供期的日线成交额：从腾讯历史 K 线把 `stock_kline.amount` 填回去。

背景：日线爬虫自 2026-05-21 起换成腾讯/新浪的 K 线接口，而**这两个接口都不给成交额**，
于是 2026-05-22 之后的 amount 全是空，下游（流动性、板块热力）只能按 volume×close×100
估算。当日新数据已由 `scripts/merge_snapshot_amount.py` 从交易所盘后快照精确回写，
这里补的是**历史那段空窗**。

为什么用腾讯 `newfqkline`：
- 爬虫走的 `ifzq.gtimg.cn/appstock/app/fqkline/get` 只有 6 列（开收高低量），确实没有成交额；
  换成 `proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get` 是 10 列，下标 8 是成交额、
  单位**万元**，下标 7 是换手率。
- 对得上原值：000002 在 2026-10-09 这里给 2355334800.00 元，库里（东财 f57 精确值）
  2355334767.01 元，差 33 元 = 万元两位小数取整；2025-02-17、2025-12-09 同样只差几十元。
  也就是精度到百元级，比 volume×close 估算（±3%）高两个数量级。
- 本机到东财 `push2his` 会被随机 RST（2026-10-15 实测 3 线程 2 只/秒几乎全挂、串行也只有
  4/6），而腾讯这台机器一直在爬、不限流；股票、科创板、场内基金都覆盖。
  网易接口已死（502 / 域名不解析），新浪资金流的成交额要靠「净额/占比」反推且对不上，都不用。

口径：
- 只写 amount，不改价格/量；库里这根 K 线已经有值就跳过——快照/东财给的是元到分，
  腾讯这里只到百元，不能拿粗的盖准的；`upsert=False`，库里没有这根 K 线就不凭空造行；
- amount<=0 跳过（停牌日），别把 0 写成「真·零成交」；
- 每条都算 `成交额 / (库里 volume×100×close)`：贴着 1 说明库里的量是「手」，贴着 0.01 说明是「股」。
  2026-10-15 用盘后快照定位到 **科创板 688 字头在库里存的是「股」**（爬虫直接把腾讯的单位存了：
  快照 `成交额/(volume×price)` 主板/创业/ETF 都是 100，688 是 1.01）。这类行照样写成交额——
  成交额是交易所原值，跟量的单位无关——但单独计数，等另做一次 ÷100 的补数才能修掉量的口径。
  两个带都不沾的（比如额没换算成元）才判「量纲不对」，只告警不写入。
- 幂等 + 断点续跑：重跑默认跳过「窗口内成交额已过半有值」的代码，只补上一轮没成功的
  那批（要整段重抓加 `--redo`）。

写完必须 `python scripts/build_kline_features.py --full` 重建 parquet——增量路径只追加新交易日，
不会把补回来的历史 amount 带进特征。

用法:
    python scripts/backfill_kline_amount.py --beg 20260522 --end 20261011 --codes 000002 --dry-run
    python scripts/backfill_kline_amount.py --beg 20260522 --end 20261011
"""

import argparse
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from random import random
from threading import Lock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
from pymongo import UpdateOne

from database import get_db

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"

URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
# 报文列：0 日期 1 开 2 收 3 高 4 低 5 成交量 6 {} 7 换手率(%) 8 成交额(万元)
DATE_COL, AMOUNT_COL = 0, 8
WAN = 1e4                      # 腾讯成交额单位是万元
MAX_RETRIES = 3
RETRY_BACKOFF = 0.5
DEFAULT_RATE = 6.0
DEFAULT_WORKERS = 4
# 连续这么多只全部失败 = 对端开始掐，歇一会儿再发，否则只会把限流窗口一直续长。
DEFAULT_BREAKER = 20
DEFAULT_COOLDOWN = 60.0
BATCH_FLUSH = 5000
SANITY_LOW, SANITY_HIGH = 0.5, 2.0        # 库里的 volume 是「手」
SHARES_LOW, SHARES_HIGH = 0.005, 0.02     # 库里的 volume 是「股」（科创板，见模块开头）
STAR_PREFIXES = ("688", "689")            # 689 是 CDR（九号公司），腾讯同样给「股」

logger = logging.getLogger("backfill_kline_amount")


class RateLimiter:
    """全局时间槽：每个请求占 1/RATE 秒，与工作线程数无关。"""

    def __init__(self, rate: float):
        self._interval = 1.0 / rate
        self._next = 0.0
        self._lock = Lock()

    def wait(self) -> None:
        with self._lock:
            slot = max(time.monotonic(), self._next)
            self._next = slot + self._interval
        time.sleep(max(0.0, slot - time.monotonic()))

    def penalize(self, seconds: float) -> None:
        """把下一个时间槽整体推后：限流休眠必须由发请求的人执行，
        放在结果消费循环里 sleep 没用——工作线程会立刻取走下一只。"""
        with self._lock:
            self._next = max(self._next, time.monotonic()) + seconds


def iso(day: str) -> str:
    return f"{day[:4]}-{day[4:6]}-{day[6:]}"


def symbol(code: str) -> str:
    """腾讯的行情代码前缀。`pricing._market_prefix` 是给 parquet 文件名用的，这里不能复用。"""
    market = "sh" if code[0] in "65" else "bj" if code[0] in "489" else "sz"
    return market + code


def row_count(beg: str, end: str) -> int:
    """接口按 count 从 end 往前给，给少了会漏掉窗口开头，所以按日历天折算交易日再留余量。"""
    days = (date.fromisoformat(iso(end)) - date.fromisoformat(iso(beg))).days
    return int(days * 5 / 7) + 20


def fetch(code: str, beg: str, end: str, limiter: RateLimiter):
    """返回 {日期: 成交额(元)}。腾讯没这段数据是答案，返 {}；取不到返 None。"""
    params = {"param": f"{symbol(code)},day,{iso(beg)},{iso(end)},{row_count(beg, end)},qfq"}
    last = None
    for attempt in range(1, MAX_RETRIES + 1):
        limiter.wait()
        try:
            r = requests.get(URL, params=params, timeout=10)
            r.raise_for_status()
            node = (r.json().get("data") or {}).get(symbol(code))
            if not isinstance(node, dict):
                return {}
            out = {}
            for row in (node.get("qfqday") or node.get("day") or []):
                if len(row) <= AMOUNT_COL:
                    continue
                try:
                    out[str(row[DATE_COL])[:10]] = float(row[AMOUNT_COL]) * WAN
                except (TypeError, ValueError):
                    continue
            return out
        except Exception as exc:      # 连接被掐重试三次；仍失败别把整批拖死
            last = exc
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * attempt + random() * 0.5)
    logger.warning("%s: %d 次都失败（%s）", code, MAX_RETRIES, str(last)[:80])
    return None


def pending_docs(code: str, beg: str, end: str) -> list[dict]:
    """窗口里库里已有的日线行；自检用它们自己的量价，而不是报文里的量。"""
    return list(get_db()["stock_kline"].find(
        {"code": code, "frequency": 9,
         "date": {"$gte": f"{iso(beg)} 00:00", "$lte": f"{iso(end)} 23:59"}},
        {"date": 1, "close": 1, "volume": 1, "amount": 1}))


def build_ops(code: str, amounts: dict[str, float], docs: list[dict],
              ratios: list[float]) -> tuple[list, dict]:
    ops, stat = [], {"ok": 0, "zero": 0, "bad": 0, "has": 0, "miss": 0, "shares": 0}
    for doc in docs:
        day = str(doc["date"])[:10]
        if (doc.get("amount") or 0) > 0:
            stat["has"] += 1              # 已有精确值（快照/东财），不拿百元级的粗值去盖
            continue
        if day not in amounts:
            stat["miss"] += 1             # 腾讯没有这天（停牌/退市/当时还没上市）
            continue
        amount = amounts[day]
        if amount <= 0:
            stat["zero"] += 1
            continue
        volume = float(doc.get("volume") or 0)
        close = float(doc.get("close") or 0)
        if volume > 0 and close > 0:
            ratio = amount / (volume * 100.0 * close)
            ratios.append(ratio)
            if SANITY_LOW <= ratio <= SANITY_HIGH:
                pass                       # 库里的量是手
            elif code[:3] in STAR_PREFIXES and SHARES_LOW <= ratio <= SHARES_HIGH:
                stat["shares"] += 1        # 库里的量是股（科创板），额照写
            else:
                stat["bad"] += 1
                logger.warning("%s %s: 成交额/(量×价)=%.3f 两个口径都不沾，疑量纲不对，不写入",
                               code, day, ratio)
                continue
        ops.append(UpdateOne({"code": code, "date": f"{day} 15:00", "frequency": 9},
                             {"$set": {"amount": amount}}, upsert=False))
        stat["ok"] += 1
    return ops, stat


def prepare(code: str, beg: str, end: str, limiter: RateLimiter,
            ratios: list[float]) -> tuple[str, list, dict]:
    amounts = fetch(code, beg, end, limiter)
    if amounts is None:
        return code, [], {"failed": 1}
    ops, stat = build_ops(code, amounts, pending_docs(code, beg, end), ratios)
    return code, ops, stat


def universe(beg: str, end: str, skip_done: bool) -> list[str]:
    """这段时间在库里有日线行的代码（没行的票不用抓，回补也落不了地）。

    `skip_done` 是断点续跑：窗口内成交额过半有值就视为已补过、直接跳过，这样重跑只补
    上一轮没成功的少数代码，不必把 6900 只再拉一遍。停牌过半的票会被反复重试，无害。
    """
    cur = get_db()["stock_kline"].aggregate(
        [{"$match": {"frequency": 9,
                     "date": {"$gte": f"{iso(beg)} 00:00", "$lte": f"{iso(end)} 23:59"}}},
         {"$group": {"_id": "$code", "rows": {"$sum": 1},
                     "filled": {"$sum": {"$cond": [{"$gt": ["$amount", 0]}, 1, 0]}}}},
         {"$sort": {"_id": 1}}], allowDiskUse=True)
    codes, done = [], 0
    for d in cur:
        if skip_done and d["filled"] * 2 >= d["rows"]:
            done += 1
            continue
        codes.append(d["_id"])
    if done:
        logger.info("跳过 %d 只：窗口内成交额已过半有值（上一轮补过）", done)
    return codes


def main() -> int:
    ap = argparse.ArgumentParser(description="腾讯历史 K 线回补日线成交额")
    ap.add_argument("--beg", required=True, help="起始日 YYYYMMDD")
    ap.add_argument("--end", required=True, help="结束日 YYYYMMDD")
    ap.add_argument("--codes", nargs="*", help="只处理这些代码（调试用）")
    ap.add_argument("--limit", type=int, default=0, help="最多处理 N 只（0=不限）")
    ap.add_argument("--rate", type=float, default=DEFAULT_RATE,
                    help=f"全局请求速率 只/秒（默认 {DEFAULT_RATE}）")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help=f"并发线程数（默认 {DEFAULT_WORKERS}）")
    ap.add_argument("--breaker", type=int, default=DEFAULT_BREAKER,
                    help=f"连续 N 只全部失败就休眠（默认 {DEFAULT_BREAKER}，0=关闭）")
    ap.add_argument("--cooldown", type=float, default=DEFAULT_COOLDOWN,
                    help=f"休眠秒数（默认 {DEFAULT_COOLDOWN:.0f}）")
    ap.add_argument("--redo", action="store_true",
                    help="不跳过已回补的代码，整段窗口重抓一遍")
    ap.add_argument("--dry-run", action="store_true", help="取数+自检，但不写库")
    args = ap.parse_args()

    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(
                      LOG_DIR / f"backfill_amount_{time.strftime('%Y%m%d')}.log",
                      encoding="utf-8")])

    codes = [c.split(".")[-1].zfill(6) for c in args.codes] if args.codes \
        else universe(args.beg, args.end, skip_done=not args.redo)
    if args.limit:
        codes = codes[:args.limit]
    if not codes:
        logger.error("标的池为空，退出")
        return 1
    logger.info("回补 %s~%s：%d 只，限速 %.1f 只/秒 ×%d 线程，下限 %.0f 分钟%s",
                args.beg, args.end, len(codes), args.rate, args.workers,
                len(codes) / args.rate / 60, "（dry-run）" if args.dry_run else "")

    limiter = RateLimiter(args.rate)
    total = {"ok": 0, "zero": 0, "bad": 0, "has": 0, "miss": 0, "shares": 0, "failed": 0,
             "matched": 0, "written": 0}
    ratios: list[float] = []
    failed: list[str] = []
    pending: list = []
    started = time.monotonic()
    consecutive_failed = 0
    collection = get_db()["stock_kline"]

    def flush(ops: list) -> None:
        if args.dry_run:
            total["matched"] += len(ops)
            return
        result = collection.bulk_write(ops, ordered=False)
        total["matched"] += result.matched_count
        total["written"] += result.modified_count

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(prepare, code, args.beg, args.end, limiter, ratios): code
                   for code in codes}
        for i, fut in enumerate(as_completed(futures), 1):
            code, ops, stat = fut.result()
            if stat.get("failed"):
                total["failed"] += 1
                failed.append(code)
                consecutive_failed += 1
            else:
                consecutive_failed = 0
                for key in ("ok", "zero", "bad", "has", "miss", "shares"):
                    total[key] += stat[key]
                pending.extend(ops)
            if args.breaker and consecutive_failed >= args.breaker:
                logger.warning("连续 %d 只取不到，判定被限流，静默 %.0f 秒",
                               consecutive_failed, args.cooldown)
                limiter.penalize(args.cooldown)
                consecutive_failed = 0
            if len(pending) >= BATCH_FLUSH:
                flush(pending)
                pending = []
            if i % 500 == 0 or i == len(codes):
                logger.info("进度 %d/%d 用时 %.0fs | 待写 %d 命中 %d 写入 %d | 已有值 %d "
                            "腾讯无此日 %d 量为股 %d 停牌0 %d 自检拦 %d 失败 %d",
                            i, len(codes), time.monotonic() - started, total["ok"],
                            total["matched"], total["written"], total["has"],
                            total["miss"], total["shares"], total["zero"],
                            total["bad"], total["failed"])

    if pending:
        flush(pending)

    if ratios:
        ratios.sort()
        q = lambda p: ratios[min(len(ratios) - 1, int(len(ratios) * p))]
        logger.info("成交额/(量×价) 分位：p5=%.3f p50=%.3f p95=%.3f"
                    "（贴着 1 = 库里的量是手，贴着 0.01 = 存成了股）",
                    q(0.05), q(0.5), q(0.95))
    logger.info("完成：%d 只｜写入 %d 根（命中 %d）/ 已有精确值 %d 根 / 腾讯无此日 %d 根；"
                "库里量是股的 %d 根（科创板，要单独补数）/ 停牌0 %d、自检拦 %d、抓取失败 %d",
                len(codes), total["written"], total["matched"], total["has"],
                total["miss"], total["shares"], total["zero"], total["bad"], total["failed"])
    if failed:
        logger.info("失败代码（重跑本脚本即可补）：%s", " ".join(failed[:80]))
    logger.info("下一步：python scripts/build_kline_features.py --full，否则补回的成交额进不了 parquet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
