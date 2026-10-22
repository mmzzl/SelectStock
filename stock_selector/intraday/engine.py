"""intraday/engine.py — 盘中策略计算引擎：tick 流 → 分钟指标 → 信号 → 风控 → 最终决策。

链路里「策略计算引擎」那一格。它不拉行情（轮询服务的事）、也不发消息（告警进程的事），
只做判定；但每条信号出来都要过 `risk.RiskEngine`——和盘后选股内核同一个类、同一套规则，
所以「盘中说可以买」和「选股建议买」不会是两套数。被风控拦下的信号照样落盘、照样进
`rt:decisions`（带 `verdict=block` 和拦它的规则名），只是不推消息：告警要能说清
「刚才有个信号为什么没推给你」。

三处成本控制在配置里（`EngineConfig`），全市场 6600 只的盘中计算很容易变成自我 DoS：
- 一轮只评估本轮有 tick 的代码，且最多 `max_eval` 只跑 talib；
- 分钟指标只在新条收口时重算（每只票每分钟最多一次），价格类信号用快照判、不跑 talib；
- 日线宽表（流动性/冲击/风险分/压力位都要它）**只在有信号要过风控时才读**，并按 LRU 缓存。

启动时把 `rt:ticks` 里已有的历史全部回放成分钟条，但回放部分**不产信号**——那些价格是
几分钟前的，此刻报出来只会诱导一个下不到的单。回放的深度由 `bus.DEFAULT_MAXLEN` 限死：320 只池
只剩最近约 10 分钟，不足 `min_bars`，所以**盘中重启 = 均线/MACD/RSI/BOLL 这类交叉信号先哑火
一小时**（等分钟条重新攒够），只有价格类信号立刻可用。要保住整段技术面就等收盘后重启。
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field

import pandas as pd

from ..realtime import bus, session
from ..risk import build_context
from ..risk.context import LOT
from ..risk.snapshot import normalize
from . import events, indicators, signals, store
from .bars import BarBuilder, minute_of

logger = logging.getLogger(__name__)

# 压力位/支撑位 + 风控要用的日线列；ML 腿的列另向 risk.data_requirements() 声明
DAY_COLUMNS = ("date", "close", "high", "low", "volume", "amount", "pct", "lu", "res20", "sup20")
DAY_ROWS = 250                    # 流动性只用 20 根、关键位取最后一根，250 留足余量
MAX_BOOK_RATIO = 99.0             # 买/卖五档比的上钳：涨停封板时卖档为空，比值是无穷大
EVENT_REFRESH = 600               # 公告是盘中陆续进的，事件表十分钟刷一次


@dataclass
class EngineConfig:
    interval: float = 5.0             # 读流间隔（与轮询服务同拍或略慢即可）
    read_batch: int = 2000            # 一轮最多读多少条 tick
    max_eval: int = 300               # 一轮最多给多少只票算 talib 分钟指标
    cooldown: int = 300               # 同票同信号的重复抑制窗口（秒）：日级去重的退路
    once_per_day: bool = True         # 同票同信号当天只报一次（买入/卖出同口径），跨天才放开
    replay_horizon: int = 60          # 比现在旧过这个秒数的 tick 只补条、不判信号
    flow_window: int = 5              # 打单流入的滚动窗口（分钟）
    kline_cache: int = 32             # 日线宽表 LRU 只数
    max_len: int = bus.DECISION_MAXLEN
    held: dict = field(default_factory=dict)   # {代码: 已持有市值(元)}，单票额度要扣它
    # 「今天能不能做这笔」的三项账户事实，由池从 app.db 一起带进来（--from-app）。
    # 全空 = 手工指定池的调试跑法：风控那三条新规则一律按「判不了就放行」走，
    # 不会把冒烟测试拦成零决策。
    ranks: dict = field(default_factory=dict)      # {代码: 批次名次（0 起）}
    bought_today: frozenset = field(default_factory=frozenset)
    cash_left: float | None = None                 # 当日可用资金（元）

    @classmethod
    def from_dict(cls, raw: dict | None) -> "EngineConfig":
        from dataclasses import fields

        data = dict(raw or {})
        known = {f.name for f in fields(cls)}
        unknown = sorted(k for k in data if k not in known)
        if unknown:
            logger.warning("盘中引擎配置里的未知项已忽略: %s", ", ".join(unknown))
        return cls(**{k: v for k, v in data.items() if k in known})


class IntradayEngine:
    """一个可注入时钟与数据源的盘中判定循环；`drain()` 是心脏，`run()` 只是外壳。"""

    def __init__(self, client, pool: list[str], risk, *, cfg: EngineConfig | None = None,
                 signal_cfg: signals.SignalConfig | None = None,
                 now=dt.datetime.now, sleeper=time.sleep, clock=time.monotonic,
                 kline_loader=None, event_loader=events.load, out_dir=None,
                 to_mongo: bool = False, pool_loader=None):
        self.client = client
        self.pool = [str(c).zfill(6) for c in pool]
        self._pool_set = set(self.pool)
        self.pool_loader = pool_loader
        self.risk = risk
        self.cfg = cfg or EngineConfig()
        self.signal_cfg = signal_cfg or signals.SignalConfig()
        self.now = now
        self.sleeper = sleeper
        self.clock = clock
        self.out_dir = out_dir
        self.to_mongo = to_mongo
        self.event_loader = event_loader
        self._kline_loader = kline_loader
        self.reset()

    # —— 生命周期 ——

    def reset(self, reason: str = "init") -> None:
        """换日/重启时清掉全部当日状态：累计量基线、指标、打单窗口、冷却表都不能跨日留。"""
        logger.info("盘中引擎清空状态（%s）", reason)
        self.cursor = "0-0"
        self.bars = BarBuilder()
        self.day = self.now().strftime("%Y-%m-%d")
        self._snap: dict[str, indicators.Snapshot] = {}
        self._cum: dict[str, tuple[float, float]] = {}
        self._flow: dict[str, deque] = {}
        self._last_price: dict[str, float] = {}
        self._bid_ratio: dict[str, float] = {}
        self._emitted: dict[str, float] = {}
        # {(票, 族名): (上次放行的方向, 时刻)}：族是「同一指标的相反两面」，两只票共用一个族名会互相拦
        self._family_last: dict[tuple[str, str], tuple[str, float]] = {}
        self._kline_cache: OrderedDict[str, object] = OrderedDict()
        self._day_ctx: dict[str, dict] = {}
        self._events_by_code: dict[str, list] = {}
        self._events_at = 0.0

    def _refresh_pool(self) -> None:
        """换日重取监控池（`pool_loader` 给了才生效）：代码与持仓市值都跟着 app.db 走。

        选股批次盘后才产，持仓也在 App 里改；常驻进程只在启动时读一次的话，周二还在按周一
        的名单算指标、按昨天的已用额度放行买入。刷新失败沿用上一批——「池空到一只都不看」
        必须是人来决定的事，不该由一次读库失败替运维做。
        """
        if self.pool_loader is None:
            return
        try:
            plan = self.pool_loader()
        except Exception as exc:
            logger.warning("盘中池刷新失败（%s），沿用上一批 %d 只", exc, len(self.pool))
            return
        codes = [str(c).zfill(6) for c in (getattr(plan, "codes", None) or [])]
        if not codes:
            logger.warning("盘中池刷新为空，沿用上一批 %d 只", len(self.pool))
            return
        self.pool = codes
        self._pool_set = set(codes)
        self.cfg.held = dict(getattr(plan, "held", None) or {})
        self.cfg.ranks = dict(getattr(plan, "ranks", None) or {})
        self.cfg.bought_today = frozenset(getattr(plan, "bought_today", None) or ())
        self.cfg.cash_left = self._cash_left(plan)
        logger.info("盘中引擎池刷新 → %d 只，%d 只按持仓市值占用额度，今日买入 %d 只（T+1 锁卖出）",
                    len(codes), len(self.cfg.held), len(self.cfg.bought_today))

    def _cash_left(self, plan) -> float | None:
        """当日可用资金：优先用池算好的（它知道持仓成本），没有就按总资金扣本地 held。

        `plan` 可能是测试注入的假对象（没有 `cash_left`），所以两条路都要能走。
        """
        capital = getattr(self.risk.cfg, "capital", 0.0) if self.risk else 0.0
        calc = getattr(plan, "cash_left", None)
        if callable(calc):
            return calc(capital)
        if not capital:
            return None
        return max(0.0, float(capital) - sum(self.cfg.held.values()))

    def _kline(self, code: str):
        """读日线宽表（LRU）。风控的流动性/冲击/风险分与突破压力位共用这一份。"""
        if self._kline_loader is None:
            from ..datasource.parquet_source import ParquetSource

            columns = list(DAY_COLUMNS)
            ml_columns, ml_rows = self.risk.data_requirements()
            columns += [c for c in ml_columns if c not in columns]
            self._kline_loader = ParquetSource(
                {"columns": columns, "rows": max(ml_rows, DAY_ROWS)}).get_kline
        if code in self._kline_cache:
            self._kline_cache.move_to_end(code)
            return self._kline_cache[code]
        frame = self._kline_loader(code)
        while len(self._kline_cache) >= self.cfg.kline_cache:
            self._kline_cache.popitem(last=False)
        self._kline_cache[code] = frame
        return frame

    def _day_level(self, code: str) -> dict:
        """20 日压力位/支撑位取基准日最后一根（形态列，见 features/patterns.py 的 res20/sup20）。"""
        if code not in self._day_ctx:
            frame = self._kline(code)
            level: dict = {}
            if frame is not None and not frame.empty:
                row = frame.iloc[-1]
                for key in ("res20", "sup20"):
                    value = row.get(key) if key in frame.columns else None
                    if pd.notna(value):
                        level[key] = float(value)
            self._day_ctx[code] = level
        return self._day_ctx[code]

    # —— 一轮 ——

    def drain(self) -> dict:
        """读一批 tick → 更新条与盘口累计 → 评估信号 → 过风控 → 落盘 + 进决策流。"""
        moment = self.now()
        if moment.strftime("%Y-%m-%d") != self.day:
            self.reset(reason=f"换日 {self.day} → {moment:%Y-%m-%d}")
            self._refresh_pool()
        started = self.clock()

        entries = bus.read_ticks(self.client, self.cursor, self.cfg.read_batch)
        now_ts = moment.timestamp()
        touched: dict[str, dict] = {}
        sealed: set[str] = set()
        replayed = 0
        skipped = 0
        for entry_id, raw in entries:
            self.cursor = entry_id
            tick = _decode(raw)
            code = str(tick.get("code") or "")
            if not code or not tick.get("price"):
                continue
            code = code.zfill(6)
            # 池外的票不建条也不评估：上游轮询可能还在发全市场，而「该给谁算指标」由这里的池决定。
            # 空池 = 没给池口径，不是「一只都不监控」，所以这时不过滤。
            if self._pool_set and code not in self._pool_set:
                skipped += 1
                continue
            if self.bars.apply(code, tick):
                sealed.add(code)
            if now_ts - float(tick.get("ts") or 0) > self.cfg.replay_horizon:
                replayed += 1                   # 历史回放：只补条，价格已经过时了
                continue
            touched[code] = tick
            self._track_flow(code, tick)

        quotes = bus.latest(self.client, list(touched)) if touched else {}
        rows = []
        evaluated = 0
        for code, tick in touched.items():
            if evaluated >= self.cfg.max_eval:
                break                            # 本轮预算用完，剩下的票下一轮再说
            evaluated += 1
            quote = quotes.get(code) or _as_quote(tick)
            # 腾讯快照只有涨跌停**价**，贴板标记要在这里用风控同一个 limit_hit 算出来，
            # 否则「涨停买不进」那条规则读到缺键的字典会静默放行。
            quote = normalize(str(code), quote)
            snap = self._refresh(code, sealed, quote)
            for signal in self._new_signals(self._view(code, quote, snap, tick)):
                rows.append(self._review(signal, quote, moment))

        if rows:
            if self.to_mongo:
                store.append_mongo(rows)
            path = store.append_parquet(rows, self.day, self.out_dir)
            bus.emit_stream(self.client, bus.KEY_DECISIONS, rows, maxlen=self.cfg.max_len)
            blocked = sum(1 for row in rows if row["verdict"] == "block")
            logger.info("本轮决策 %d 条（放行 %d，拦截 %d）→ %s",
                        len(rows), len(rows) - blocked, blocked, path)

        stats = {"ticks": len(entries), "replayed": replayed, "changed": len(touched),
                 "skipped": skipped, "pool": len(self._pool_set),
                 "evaluated": evaluated, "decisions": len(rows),
                 "blocked": sum(1 for row in rows if row["verdict"] == "block"),
                 "cursor": self.cursor, "elapsed_s": round(self.clock() - started, 3),
                 "at": round(now_ts, 3), "status": "ok"}
        bus.heartbeat(self.client, stats, key=bus.KEY_ENGINE_HEARTBEAT)
        logger.debug("引擎本轮 %s", stats)
        return stats

    def _refresh(self, code: str, sealed: set[str], quote: dict):
        """新条收口时重算指标并写进 redis（「redis 存实时指标」那一格）；没收口就沿用上次结果。"""
        if code not in sealed:
            return self._snap.get(code)
        snap = indicators.compute(self.bars.closed(code), self.signal_cfg.min_bars)
        if snap is None:
            self._snap.pop(code, None)
            return None               # 预热不足：本轮只跑价格类信号，不给半温的指标
        self._snap[code] = snap
        bus.put_metrics(self.client, code, {
            "minute": snap.minute, "bars": snap.bars,
            "price": quote.get("price"), "pct": quote.get("pct"),
            "vol_ratio": quote.get("vol_ratio"), "turnover_pct": quote.get("turnover_pct"),
            **snap.current,
        })
        return snap

    def _view(self, code: str, quote: dict, snap, tick: dict) -> signals.View:
        self._ensure_events()
        ratio = book_ratio(quote)
        prev_ratio = self._bid_ratio.get(code)
        self._bid_ratio[code] = ratio
        minute = minute_of(tick["ts"]) if tick.get("ts") is not None else 0
        view = signals.View(code=code, name=quote.get("name") or "", quote=quote, snap=snap,
                            day=self._day_level(code),
                            flow={"inflow_amount": self._inflow(code, minute),
                                  "window_min": self.cfg.flow_window,
                                  "bid_ratio": ratio, "prev_bid_ratio": prev_ratio},
                            events=self._events_by_code.get(code, []),
                            last_price=self._last_price.get(code), minute=minute,
                            # 卖出方向只对持仓判定：池子里绝大多数是选股命中，没有仓位就没什么可卖
                            held=code in self.cfg.held)
        self._last_price[code] = quote.get("price")
        return view

    def _ensure_events(self) -> None:
        """事件表十分钟刷一次：公告是盘中陆续进的，一直不刷会漏；每轮都刷则太贵。"""
        stamp = self.now().timestamp()
        if stamp - self._events_at < EVENT_REFRESH:
            return
        self._events_at = stamp
        self._events_by_code = self.event_loader(self.day, self.pool) or {}

    # —— 盘口累计 ——

    def _track_flow(self, code: str, tick: dict) -> None:
        """外盘/内盘是当日累计值，增量 = 本拍 − 上一拍；净主动买入额 = (外−内)×100 股×现价。"""
        outer, inner = tick.get("outer_volume"), tick.get("inner_volume")
        if outer is None or inner is None or tick.get("ts") is None:
            return
        minute = minute_of(tick["ts"])
        prev = self._cum.get(code)
        self._cum[code] = (outer, inner)
        if prev is None:
            return                          # 基线刚立起来，这段成交归不到分钟上
        d_out, d_in = outer - prev[0], inner - prev[1]
        if d_out < 0 or d_in < 0:
            return                          # 换日/重传：增量不可信，丢掉而不是记成负数
        net = (d_out - d_in) * LOT * (tick.get("price") or 0.0)
        buf = self._flow.setdefault(code, deque())
        buf.append((minute, net))
        while buf and minute - buf[0][0] > self.cfg.flow_window:
            buf.popleft()

    def _inflow(self, code: str, minute: int) -> float | None:
        buf = self._flow.get(code)
        if not buf:
            return None
        recent = [amount for stamp, amount in buf if minute - stamp <= self.cfg.flow_window]
        return round(sum(recent), 2) if recent else None

    # —— 信号 → 风控 → 决策 ——

    def _new_signals(self, view: signals.View) -> list[signals.Signal]:
        """评估 + 去重：同一只票同一类信号当天只形成一次决策，买入和卖出同一个口径。

        被去重吃掉的算不上「风控拒绝」，所以不落盘、不进决策流——它压根没变成一条新决策。
        """
        stamp = self.now().timestamp()
        out = []
        for signal in signals.evaluate(view, self.signal_cfg):
            if self._should_emit(signal, stamp):
                out.append(signal)
        return out

    def _should_emit(self, signal: signals.Signal, stamp: float) -> bool:
        """日级去重优先，退回进程内 `cooldown` 窗口。

        去重表写在 redis：引擎重启把上午报过的信号再报一遍，整条告警就没有可信度了。中间件
        抖动时宁可退化成「5 秒内不重复」，也不能让链路卡在这里——告警宁多报，不能不报。
        """
        if self.cfg.once_per_day:
            try:
                marked = bus.mark_once(self.client, self.day, signal.key)
            except Exception as exc:
                logger.warning("当日去重表写不进去，退回冷却窗口判定: %s", exc)
            else:
                if marked:
                    self._emitted[signal.key] = stamp
                return marked
        last = self._emitted.get(signal.key)
        if last is not None and stamp - last < self.cfg.cooldown:
            return False
        self._emitted[signal.key] = stamp
        return True

    def _review(self, signal: signals.Signal, quote: dict, moment: dt.datetime) -> dict:
        """单条信号过风控：能不能成交、买多少、什么价成交，都由风控定，不由信号定。

        镜像族的历史在这里交给风控：同一个指标刚给过反方向，`family_flip` 要拿「上一条是什么、
        隔了多久」才能判这是反手还是抖动。只记放行的那条——被拦掉的那条没有成为建议，不该
        再把下一个方向也拖住。
        """
        stamp = moment.timestamp()
        prev = self._family_last.get((signal.code, signal.family))
        ctx = build_context(signal.code, signal.action, name=signal.name,
                            signal_price=signal.price, day=self.day,
                            kline=self._kline(signal.code), quote=quote or None,
                            position_value=float(self.cfg.held.get(signal.code, 0.0)),
                            pool_rank=self.cfg.ranks.get(signal.code),
                            bought_today=signal.code in self.cfg.bought_today,
                            cash_left=self.cfg.cash_left,
                            family=signal.family,
                            prev_action=prev[0] if prev else None,
                            prev_age_s=stamp - prev[1] if prev else None)
        decision = self.risk.review(ctx)
        if not decision.blocked:
            self._family_last[(signal.code, signal.family)] = (signal.action, stamp)
        row = store.row(signal, decision, ts=stamp, day=self.day,
                        pushed=not decision.blocked,
                        metrics={"quote_time": quote.get("quote_time", "")})
        if decision.blocked:
            logger.info("信号 %s/%s 被风控拦下：%s", signal.code, signal.kind, decision.reason)
        return row

    # —— 外壳 ——

    def run(self, rounds: int | None = None) -> None:
        """常驻循环：非交易时段睡到下一次开盘，盘内按 `interval` 读流。"""
        done = 0
        while rounds is None or done < rounds:
            moment = self.now()
            if not session.in_trading_window(moment):
                wait = session.sleep_seconds(moment)
                logger.info("不在交易时段，引擎休眠 %.0f 秒", wait)
                self.sleeper(wait)
                done += 1
                continue
            self.drain()
            self.sleeper(self.cfg.interval)
            done += 1


def book_ratio(quote: dict) -> float | None:
    """五档委托量比：Σ买量 / Σ卖量。一档都没有时返回 None（判不了，不猜）。

    一侧为空时比值会爆炸：涨停封死卖档为空 → 钳到 `MAX_BOOK_RATIO`（算托单），跌停封死买档
    为空 → 0.0（算压单）。两个方向都是有意义的极值，不是缺失。
    """
    bids, asks = quote.get("bids") or [], quote.get("asks") or []
    if not bids and not asks:
        return None
    bid = float(sum(level[1] for level in bids))
    ask = float(sum(level[1] for level in asks))
    if ask == 0:
        return MAX_BOOK_RATIO
    return round(min(bid / ask, MAX_BOOK_RATIO), 4)


def _decode(raw: dict) -> dict:
    """stream field 全是字符串：能转数的转回来，转不了的原样留着。"""
    return {key: _number(value) for key, value in raw.items()}


def _number(value):
    text = str(value)
    for cast in (int, float):
        try:
            return cast(text)
        except ValueError:
            continue
    return text


def _as_quote(tick: dict) -> dict:
    """`rt:quote` 里读不到该代码时（hash 写失败等边角）用 tick 拼一份最小快照。

    `source` 标成 tick，让下游知道这不是完整快照——没有五档，滑点规则会走 `min_spread_bp`
    的下限而不是真实价差，宁可保守 estimate 也不假装看得见盘口。
    """
    return {key: value for key, value in tick.items() if key != "ts"} | {
        "source": "tick", "bids": [], "asks": []}
