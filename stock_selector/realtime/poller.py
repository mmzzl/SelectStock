"""realtime/poller.py — 快照轮询循环：拉行情 → 判休市 → 只把变化推上 redis。

为什么只推「变化」：免费快照 5 秒一拍、全市场 6600 只，原样转发就是每小时 4700 万条 stream
记录，而策略引擎真正关心的是「谁动了」。`rt:quote` 已经存了全量最新值，流里只留差分，
两边合起来信息不丢。

休市判定用快照自带的时间戳而不是日历表：交易所给自己的更新时刻如果还不是今天，说明现在
根本没有行情（节假日/未开盘）。这时**一条都不发**——把上个交易日的价当实时价推给策略，
比不发更危险。
"""

from __future__ import annotations

import datetime as dt
import logging
import time

from ..quote import tencent
from . import bus, session

logger = logging.getLogger(__name__)

# 变化判定只看这几个字段：五档、市值每次请求都会变，但不构成「新的一拍」；
# 而外盘/内盘是盘口监控的唯一依据（打单流入），不带就只能回头再查一次快照。
# 五档不进流（体积太大），引擎要用时直接读 rt:quote 里的最新快照。
TICK_FIELDS = ("price", "pct", "volume", "amount", "turnover_pct", "vol_ratio",
               "limit_up", "limit_down", "outer_volume", "inner_volume", "quote_time")

MAX_BACKOFF = 60.0          # 连续失败时的休眠上限：再大就错过行情，再小就是在刷限流


def resolve_universe(codes: list[str] | None = None, *, all_market: bool = False,
                     limit: int = 0) -> list[str]:
    """自选清单 / 全市场两种股票池，六位代码。

    全市场直接取本地指标宽表的文件名片（与选股内核同一个股票池口径），不去问行情接口——
    行情接口没有「列出全部」的能力，硬要翻代码段会漏掉新代码段（比如 920 开头的北交所）。
    自选代码排在前头并保留：新上市的票可能还没进宽表，只盯自选的人也不该被全市场池覆盖掉。
    """
    pool = [str(c).zfill(6) for c in (codes or []) if str(c).strip()]
    if all_market:
        from ..datasource.parquet_source import ParquetSource

        pool += [str(item["code"]).zfill(6) for item in ParquetSource().get_stock_list()]
    out: list[str] = []
    seen: set[str] = set()
    for code in pool:
        if code not in seen:
            seen.add(code)
            out.append(code)        # 保序去重：清单里手写重复的票不该被轮询两次
    return out[:limit] if limit else out


def to_tick(code: str, quote: dict, ts: float) -> dict:
    """快照 → 进流的那条差分（只带判定要用的字段，id 由 redis 生成）。"""
    tick = {"code": code, "ts": round(ts, 3)}
    for key in TICK_FIELDS:
        tick[key] = quote.get(key)
    return tick


def changed(prev: dict | None, quote: dict) -> bool:
    """和上一拍比是否变化。首次见到一定算变化，否则策略拿不到初始状态。"""
    if prev is None:
        return True
    return any(prev.get(key) != quote.get(key) for key in TICK_FIELDS)


class QuotePoller:
    """一轮 = 一次批量快照 + 一次上行写入。交易时段判断、休市停发、退避都在这里。

    `now`/`sleeper`/`fetch` 都可注入：常驻进程的三态（正常/休市/拉不到）只有靠假时钟和假
    行情才能测出来，否则测试只能覆盖「现在正好在盘中」这一种情况。
    """

    def __init__(self, client, codes: list[str], *, interval: float = 5.0,
                 maxlen: int = bus.DEFAULT_MAXLEN, fetch=tencent.fetch,
                 now=dt.datetime.now, clock=time.monotonic, sleeper=time.sleep):
        self.client = client
        self.codes = [str(c).zfill(6) for c in codes]
        self.interval = interval
        self.maxlen = maxlen
        self.fetch = fetch
        self.now = now
        self.clock = clock
        self.sleeper = sleeper
        self.prev: dict[str, dict] = {}
        self.rounds = 0
        self.consecutive_failures = 0
        self.backoff = interval

    def round(self) -> dict:
        """跑一拍，返回本轮统计（同时写进心跳，供运维与告警链路判断链路是否活着）。"""
        self.rounds += 1
        stamp = self.now()
        started = self.clock()
        quotes = self.fetch(self.codes) or {}
        elapsed = round(self.clock() - started, 3)
        ts = round(stamp.timestamp(), 3)

        stats = {"round": self.rounds, "requested": len(self.codes), "quotes": len(quotes),
                 "elapsed_s": elapsed, "at": ts}
        if not quotes:
            # 一只都没拉到 = 数据源故障（限流/断网），不能当成「今天没票」静默过去
            self.consecutive_failures += 1
            self.backoff = min(MAX_BACKOFF, max(self.interval, self.backoff * 2))
            stats.update(status="fetch_failed", sleep_s=self.backoff)
            logger.warning("快照整轮为空（%d 只，连续 %d 次），退避到 %.1f 秒",
                           len(self.codes), self.consecutive_failures, self.backoff)
            bus.heartbeat(self.client, stats)
            return stats

        fresh = {c: q for c, q in quotes.items()
                 if session.snapshot_is_today(q.get("quote_time"), stamp)}
        if not fresh:
            stats.update(status="market_closed", sleep_s=session.sleep_seconds(stamp))
            logger.info("快照时间戳都不是 %s，判定休市/未开盘，停发 tick（休眠 %.0f 秒）",
                        stamp.date(), stats["sleep_s"])
            bus.heartbeat(self.client, stats)
            return stats

        ticks = []
        for code, quote in fresh.items():
            if changed(self.prev.get(code), quote):
                ticks.append(to_tick(code, quote, ts))
            self.prev[code] = quote

        written = bus.put_quotes(self.client, fresh)
        emitted = bus.emit_ticks(self.client, ticks, maxlen=self.maxlen)
        self.consecutive_failures = 0
        self.backoff = self.interval
        stats.update(status="ok", stale=len(quotes) - len(fresh), ticks=emitted,
                     quotes=written, sleep_s=self.interval)
        bus.heartbeat(self.client, stats)
        logger.debug("本轮 %s", stats)      # 5 秒一拍的常规日志不上 INFO，看心跳用 --status
        return stats

    def run(self, rounds: int | None = None) -> None:
        """常驻循环。`rounds` 只给测试与冒烟用，None 表示一直跑到进程被停。"""
        done = 0
        while rounds is None or done < rounds:
            if not session.in_trading_window(self.now()):
                wait = session.sleep_seconds(self.now())
                logger.info("不在交易时段，休眠 %.0f 秒", wait)
                self.sleeper(wait)
                done += 1
                continue
            stats = self.round()
            self.sleeper(stats.get("sleep_s", self.interval))
            done += 1
