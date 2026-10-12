"""realtime/session.py — A 股交易时段与「快照到底是不是今天」的判定。

为什么时段判断要独立成模块：轮询服务、盘中策略引擎、告警链路都要同一套「现在该不该跑」，
各写一遍迟早会不一致（比如有人忘了午间休市）。

节假日不在这里硬编码表：沪深休市没有本地数据可依，但腾讯快照的 `quote_time` 是**交易所
自己打的更新时刻**（`20261012150003`）。休市日/未开盘时它停在上一个交易日，于是
「快照时间不是今天」就是最可靠的休市信号——比维护一张会过期的节假日表稳。
"""

from __future__ import annotations

import datetime as dt

# 集合竞价尾段就开始盯：09:25 之后价格已经是可成交价；收盘后多留 5 分钟收尾盘快照
MORNING = (9 * 60 + 25, 11 * 60 + 30)
AFTERNOON = (13 * 60, 15 * 60 + 5)

MAX_SLEEP = 900           # 休市时的最长休眠（秒）：睡过头会错过开盘，到点重新判一次


def clock_now(now: dt.datetime | None = None) -> dt.datetime:
    return now or dt.datetime.now()


def in_trading_window(now: dt.datetime | None = None) -> bool:
    """现在是否在可成交时段内（周末直接 False；节假日靠 snapshot_is_today 判）。"""
    moment = clock_now(now)
    if moment.weekday() >= 5:
        return False
    minutes = moment.hour * 60 + moment.minute
    return MORNING[0] <= minutes < MORNING[1] or AFTERNOON[0] <= minutes < AFTERNOON[1]


def seconds_to_next_window(now: dt.datetime | None = None) -> int:
    """距下一个交易时段开头还有多久：休市时按这个值休眠，开盘前一拍自然被唤醒。"""
    moment = clock_now(now)
    today_minutes = moment.hour * 60 + moment.minute
    starts = [MORNING[0], AFTERNOON[0]]
    for start in starts:
        if today_minutes < start:
            return max(1, (start - today_minutes) * 60 - moment.second)
    # 当天已收：睡到明天 09:25（周末则由调用方的 MAX_SLEEP 兜住，逐日复判）
    tomorrow = (moment + dt.timedelta(days=1)).replace(
        hour=MORNING[0] // 60, minute=MORNING[0] % 60, second=0, microsecond=0)
    return max(1, int((tomorrow - moment).total_seconds()))


def sleep_seconds(now: dt.datetime | None = None) -> int:
    """休眠时长：离开盘还远就睡满 MAX_SLEEP，临近开盘就睡到点。"""
    return min(MAX_SLEEP, seconds_to_next_window(now))


def snapshot_is_today(quote_time: str, now: dt.datetime | None = None) -> bool:
    """快照时间戳是不是今天：`20261012150003` → 2026-10-12。拿不到时间戳按「不是今天」处理，
    宁可不发也不要把上个交易日的价当实时价推给策略。"""
    raw = str(quote_time or "").strip()
    if len(raw) < 8 or not raw[:8].isdigit():
        return False
    return raw[:8] == clock_now(now).strftime("%Y%m%d")
