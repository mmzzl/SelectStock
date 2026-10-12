"""intraday/bars.py — 分钟条：把「当日累计量」的快照还原成 OHLCV 分钟线。

腾讯快照里的 volume/amount 是**当天累计值**（量单位手、额单位元），不是这一拍的增量，
分钟条只能由相邻两拍的差值得到；轮询 5 秒一拍，同一分钟内的几拍合并进同一根条。

两个必须防的口径坑：
- 换日：`quote_time` 前 8 位一变就是新交易日，累计量归零重新起基线，昨天的条不能留在序列里
  （否则均线跨日、量能对比跨日，全是错的）；
- 倒退：累计值理论上只增不减，真减了说明基线不可信（补数/改口径），直接把当拍当新基线，
  而不是把负增量塞进条里算出一根「负成交量」的分钟线。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

BAR_SECONDS = 60


@dataclass
class _State:
    day: str
    cum_volume: float
    cum_amount: float
    bars: deque
    cur: dict | None = None


def minute_of(ts: float) -> int:
    """落在哪一分钟（整分钟 epoch 秒）。用轮询落盘时刻而不是行情时间戳：一条 bar 的
    边界必须由**本地时钟**决定，否则行情延迟会让两拍挤进同一分钟或错开一分钟。"""
    return int(ts // BAR_SECONDS)


def day_of(tick: dict) -> str:
    """tick 自带的交易日（YYYYMMDD），拿不到时是空串——空串不参与换日判定。"""
    return str(tick.get("quote_time") or "")[:8]


class BarBuilder:
    """按代码维护当日分钟条，`capacity` 限制单只票保留多少根（240 根 = 一整个交易日）。"""

    def __init__(self, capacity: int = 240):
        self.capacity = capacity
        self.state: dict[str, _State] = {}

    def apply(self, code: str, tick: dict) -> bool:
        """写入一拍，返回「这一拍是否收口了一根新的分钟条」。"""
        price = tick.get("price")
        ts = tick.get("ts")
        if not price or ts is None:
            return False        # 没有本地时间戳就无从归分钟，宁可丢这一拍
        code = str(code)
        day, minute = day_of(tick), minute_of(ts)
        st = self.state.get(code)
        if st is None or (day and st.day != day):
            # 首见/换日：只立累计基线。盘中启动时不知道之前成交了多少，第一根条量能为 0
            # 是事实而不是缺陷，指标要预热才出数（见 indicators.MIN_BARS）。
            st = self.state[code] = _State(day=day, cum_volume=tick.get("volume") or 0.0,
                                           cum_amount=tick.get("amount") or 0.0,
                                           bars=deque(maxlen=self.capacity))
            st.cur = _bar(minute, price)
            return False

        sealed = False
        if minute > st.cur["minute"]:
            st.bars.append(dict(st.cur))
            st.cur = _bar(minute, price)
            sealed = True

        cur = st.cur
        volume = tick.get("volume")
        amount = tick.get("amount")
        if volume is not None:
            if volume < st.cum_volume:
                st.cum_volume = volume          # 基线不可信，重新起基线，本拍不计增量
            else:
                cur["volume"] += volume - st.cum_volume
                st.cum_volume = volume
        if amount is not None:
            if amount < st.cum_amount:
                st.cum_amount = amount
            else:
                cur["amount"] += amount - st.cum_amount
                st.cum_amount = amount

        cur["close"] = price
        cur["high"] = max(cur["high"], price)
        cur["low"] = min(cur["low"], price)
        return sealed

    def closed(self, code: str) -> list[dict]:
        """已收口的分钟条（升序）。指标只用这些——未收口那根的量价还在变。"""
        st = self.state.get(str(code))
        return list(st.bars) if st else []

    def current(self, code: str) -> dict | None:
        """当前这一根（未收口）：引擎判「这一分钟放量」「这一分钟跌了多少」用它。"""
        st = self.state.get(str(code))
        return dict(st.cur) if st and st.cur else None

    def series(self, code: str, include_partial: bool = False) -> list[dict]:
        st = self.state.get(str(code))
        if not st:
            return []
        out = list(st.bars)
        if include_partial and st.cur:
            out.append(dict(st.cur))
        return out

    def forget(self, code: str | None = None) -> None:
        if code is None:
            self.state.clear()
        else:
            self.state.pop(str(code), None)


def _bar(minute: int, price: float) -> dict:
    """新起一根：OHLC 先都是这一拍的价格，量能从 0 起算。"""
    return {"minute": minute, "ts": minute * BAR_SECONDS, "open": price, "high": price,
            "low": price, "close": price, "volume": 0.0, "amount": 0.0}
