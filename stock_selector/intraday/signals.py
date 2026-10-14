"""intraday/signals.py — 盘中信号：技术面 / 盘口 / 基本面事件三类，只产信号不做可否成交的判定。

三条口径（引擎和风控都依赖它，别在规则里各写各的）：

1. **每条信号都必须表态**（buy 或 sell），信息类也不例外：高换手统一按「持仓者该警惕」给 sell，
   触涨停按「想买」给 buy——后者会被风控的成交性规则明确拦住，这正是「策略只产生信号，
   决策必须独立风控」要在盘中最直白的演示。
   但**卖出方向只对持仓判定**（见 `View.held`）：池子里绝大多数是当天选出来的票，给没持有的
   票推卖出既执行不了也只是刷屏。
2. 均线/MACD/RSI/BOLL/单分钟量能一律在**收口的分钟条**上判（`indicators.Snapshot`），和日线
   收盘确认同一个逻辑：未收口那根的量价还在变，拿它判交叉会来回翻脸、一分钟报三次。
3. 价格类（涨停、量比、换手、突破日线压力位、打单、盘口挂单）用**最新快照**判，这类本来就是
   瞬时状态，等一分钟收口就晚了。

`kinds` 白名单让运维能一键关掉噪声大的那几条，不用改代码。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..risk.snapshot import limit_hit
from .indicators import Snapshot, crossed_down, crossed_up

# —— 信号种类（settings.yaml 的 intraday.kinds 用这些名字挑）——
KINDS = ("ma_golden_cross", "ma_dead_cross", "macd_golden_cross", "macd_dead_cross",
         "rsi_oversold_bounce", "rsi_overbought", "boll_break_up", "boll_break_dn",
         "volume_blast_up", "volume_surge_drop", "vol_ratio_surge", "turnover_hot",
         "breakout_resistance", "support_break", "order_flow_inflow", "order_flow_outflow",
         "bid_stack", "ask_stack", "limit_up_touch", "limit_down_touch",
         "event_bullish", "event_bearish")


@dataclass
class Signal:
    code: str
    action: str                 # buy | sell
    kind: str
    price: float | None
    reason: str
    name: str = ""
    minute: int = 0
    metrics: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        """去重键：同一只票同一类信号在冷却期内只报一次。"""
        return f"{self.code}|{self.kind}"

    def as_dict(self) -> dict:
        return {"code": self.code, "name": self.name, "action": self.action,
                "kind": self.kind, "price": self.price, "reason": self.reason,
                "minute": self.minute, "metrics": self.metrics}


@dataclass
class SignalConfig:
    min_bars: int = 60              # 指标预热：分钟条不足就只跑价格类信号
    vol_ratio: float = 3.0          # 量比
    turnover_pct: float = 10.0      # 换手率（百分数）
    blast_mult: float = 2.0         # 单分钟量/vol_ma5 的倍数
    minute_move_pct: float = 1.0    # 单分钟涨跌幅门槛（百分数，正负各用一次）
    day_drop_pct: float = -5.0      # 当日跌幅：配合放量判「放量大跌」
    day_vol_ratio: float = 2.0
    breakout_bp: float = 20.0       # 突破/跌破日线关键位的确认幅度（万分比）
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    inflow_amount: float = 5e6      # 主动净买入额（元）
    book_ratio: float = 2.0         # 五档委比：买量/卖量 达到这个倍数算压单/托单
    kinds: tuple | None = None      # None = 全开

    @classmethod
    def from_dict(cls, raw: dict | None) -> "SignalConfig":
        from dataclasses import fields

        data = dict(raw or {})
        known = {f.name for f in fields(cls)}
        unknown = sorted(k for k in data if k not in known)
        if unknown:
            import logging

            logging.getLogger(__name__).warning("盘中信号配置里的未知项已忽略: %s", ", ".join(unknown))
        if data.get("kinds"):
            data["kinds"] = tuple(data["kinds"])
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class View:
    """一次判定的全部输入：最新快照 + 分钟指标 + 日线关键位 + 盘口累计 + 当日事件。"""

    code: str
    name: str = ""
    quote: dict = field(default_factory=dict)
    snap: Snapshot | None = None
    day: dict = field(default_factory=dict)     # res20 / sup20 / pct 等日线口径
    flow: dict = field(default_factory=dict)    # inflow_amount / bid_ratio / prev_bid_ratio
    events: list = field(default_factory=list)  # 当日 watch_target 记录
    last_price: float | None = None             # 上一拍价格：判「上穿/跌破」需要它
    minute: int = 0
    held: bool = False                          # 当前是否在持仓里；False = 不判卖出方向

    @property
    def price(self) -> float | None:
        return self.quote.get("price") or None


def _hit(v: View, cfg: SignalConfig, kind: str) -> bool:
    return cfg.kinds is None or kind in cfg.kinds


def _sig(v: View, action: str, kind: str, reason: str, **metrics) -> Signal:
    return Signal(code=v.code, name=v.name or v.quote.get("name") or "", action=action,
                  kind=kind, price=v.price, reason=reason, minute=v.minute, metrics=metrics)


def evaluate(v: View, cfg: SignalConfig | None = None) -> list[Signal]:
    """跑完三类规则，返回这一拍的信号（可能为空，也可能同时给出一条买一条卖）。"""
    cfg = cfg or SignalConfig()
    out: list[Signal] = []
    for rule in (_trend, _rsi, _boll, _bars, _snapshot, _breakout, _flow, _book, _limit,
                 _events):
        out.extend(rule(v, cfg))
    if not v.held:
        # 「卖」的宾语必须是自己的持仓：选股池里的票没有可卖的仓位，这类信号一律不成立。
        out = [s for s in out if s.action != "sell"]
    return out


# —— 技术面：收口分钟条上的交叉 ——

def _trend(v: View, cfg: SignalConfig) -> list[Signal]:
    s = v.snap
    if not s:
        return []
    out = []
    close = s.current.get("close")
    if _hit(v, cfg, "ma_golden_cross") and close and crossed_up(s, "ma5", "ma10") \
            and s.current.get("ma20") and close > s.current["ma20"]:
        out.append(_sig(v, "buy", "ma_golden_cross",
                        f"5 分钟均线 {s.current['ma5']} 上穿 10 分钟 {s.current['ma10']}，"
                        f"且站上 20 分钟 {s.current['ma20']}",
                        ma5=s.current["ma5"], ma10=s.current["ma10"], ma20=s.current["ma20"]))
    if _hit(v, cfg, "ma_dead_cross") and close and crossed_down(s, "ma5", "ma10") \
            and s.current.get("ma20") and close < s.current["ma20"]:
        out.append(_sig(v, "sell", "ma_dead_cross",
                        f"5 分钟均线跌破 10 分钟，且失守 20 分钟 {s.current['ma20']}",
                        ma5=s.current["ma5"], ma10=s.current["ma10"], ma20=s.current["ma20"]))
    if _hit(v, cfg, "macd_golden_cross") and crossed_up(s, "macd_dif", "macd_dea"):
        out.append(_sig(v, "buy", "macd_golden_cross",
                        f"MACD 金叉：dif {s.current['macd_dif']} 上穿 dea {s.current['macd_dea']}",
                        dif=s.current["macd_dif"], dea=s.current["macd_dea"],
                        hist=s.current.get("macd_hist")))
    if _hit(v, cfg, "macd_dead_cross") and crossed_down(s, "macd_dif", "macd_dea"):
        out.append(_sig(v, "sell", "macd_dead_cross",
                        f"MACD 死叉：dif {s.current['macd_dif']} 下穿 dea {s.current['macd_dea']}",
                        dif=s.current["macd_dif"], dea=s.current["macd_dea"],
                        hist=s.current.get("macd_hist")))
    return out


def _rsi(v: View, cfg: SignalConfig) -> list[Signal]:
    s = v.snap
    if not s:
        return []
    now, prev = s.current.get("rsi14"), s.previous.get("rsi14")
    if now is None or prev is None:
        return []
    out = []
    if _hit(v, cfg, "rsi_oversold_bounce") and prev < cfg.rsi_oversold <= now:
        out.append(_sig(v, "buy", "rsi_oversold_bounce",
                        f"RSI 从超卖区回升 {prev:.1f}→{now:.1f}（阈值 {cfg.rsi_oversold}）",
                        rsi=now, rsi_prev=prev))
    if _hit(v, cfg, "rsi_overbought") and prev < cfg.rsi_overbought <= now:
        out.append(_sig(v, "sell", "rsi_overbought",
                        f"RSI 进入超买区 {prev:.1f}→{now:.1f}（阈值 {cfg.rsi_overbought}）",
                        rsi=now, rsi_prev=prev))
    return out


def _boll(v: View, cfg: SignalConfig) -> list[Signal]:
    s = v.snap
    if not s:
        return []
    out = []
    if _hit(v, cfg, "boll_break_up") and crossed_up(s, "close", "boll_upper"):
        out.append(_sig(v, "buy", "boll_break_up",
                        f"分钟收盘上穿布林上轨 {s.current.get('boll_upper')}",
                        close=s.current.get("close"), boll_upper=s.current.get("boll_upper")))
    if _hit(v, cfg, "boll_break_dn") and crossed_down(s, "close", "boll_lower"):
        out.append(_sig(v, "sell", "boll_break_dn",
                        f"分钟收盘跌破布林下轨 {s.current.get('boll_lower')}",
                        close=s.current.get("close"), boll_lower=s.current.get("boll_lower")))
    return out


def _bars(v: View, cfg: SignalConfig) -> list[Signal]:
    """单分钟放量拉升 / 放量大跌：量能基准用收口那根的 `vol_ma5`，涨跌幅用相邻两根收口条。

    两根都取收口值（`snap.current` / `snap.previous`），不看未收口那根——未收口的量只会随着
    这一分钟继续成交而变大，用它判「放量」就是同一根条报三次、每次倍数都不一样。
    """
    s = v.snap
    if not s:
        return []
    base, volume = s.current.get("vol_ma5"), s.current.get("volume")
    move = s.current.get("bar_pct")
    if not base or not volume or move is None:
        return []
    mult = volume / base
    if mult < cfg.blast_mult:
        return []
    out = []
    if _hit(v, cfg, "volume_blast_up") and move >= cfg.minute_move_pct:
        out.append(_sig(v, "buy", "volume_blast_up",
                        f"放量拉升：分钟量是 5 分钟均量的 {mult:.1f} 倍、涨 {move:.2f}%",
                        bar_volume=volume, vol_ma5=base, bar_pct=round(move, 3)))
    if _hit(v, cfg, "volume_surge_drop") and move <= -cfg.minute_move_pct:
        out.append(_sig(v, "sell", "volume_surge_drop",
                        f"放量大跌：分钟量是 5 分钟均量的 {mult:.1f} 倍、跌 {move:.2f}%",
                        bar_volume=volume, vol_ma5=base, bar_pct=round(move, 3)))
    return out


# —— 技术面：最新快照上的瞬时状态 ——

def _snapshot(v: View, cfg: SignalConfig) -> list[Signal]:
    q, pct = v.quote, v.quote.get("pct")
    if pct is None:
        return []
    out = []
    ratio = q.get("vol_ratio")
    if _hit(v, cfg, "vol_ratio_surge") and ratio and ratio >= cfg.vol_ratio and pct > 0:
        out.append(_sig(v, "buy", "vol_ratio_surge",
                        f"量比 {ratio:.1f} ≥ {cfg.vol_ratio:.1f} 且当日 {pct:+.2f}%（放量上攻）",
                        vol_ratio=ratio, pct=pct))
    turnover = q.get("turnover_pct")
    if _hit(v, cfg, "turnover_hot") and turnover and turnover >= cfg.turnover_pct:
        # 高换手不给买入方向：换手打到两位数时，多空分歧最大，提示的是持仓风险而不是买点
        out.append(_sig(v, "sell", "turnover_hot",
                        f"换手率 {turnover:.1f}% ≥ {cfg.turnover_pct:.1f}%（当日 {pct:+.2f}%，"
                        f"高换手后波动放大）", turnover_pct=turnover, pct=pct))
    if _hit(v, cfg, "volume_surge_drop") and pct <= cfg.day_drop_pct and ratio \
            and ratio >= cfg.day_vol_ratio:
        out.append(_sig(v, "sell", "volume_surge_drop",
                        f"放量大跌：当日 {pct:.2f}% 且量比 {ratio:.1f}", pct=pct, vol_ratio=ratio))
    return out


def _breakout(v: View, cfg: SignalConfig) -> list[Signal]:
    """突破日线压力位 / 跌破日线支撑位（res20、sup20 来自形态列，见 features/patterns.py）。

    用「上一拍还在位下、这一拍站上去了」判上穿，并加 `breakout_bp` 的确认幅度——刚碰到位
    就报会被盘口噪音反复触发。
    """
    price, last = v.price, v.last_price
    if not price or last is None:
        return []
    pad = cfg.breakout_bp / 1e4
    out = []
    res, sup = v.day.get("res20"), v.day.get("sup20")
    if _hit(v, cfg, "breakout_resistance") and res and last <= res and price > res \
            and price >= res * (1 + pad):
        out.append(_sig(v, "buy", "breakout_resistance",
                        f"上穿 20 日压力位 {res}（现价 {price}）", res20=res, price=price))
    if _hit(v, cfg, "support_break") and sup and last >= sup and price < sup \
            and price <= sup * (1 - pad):
        out.append(_sig(v, "sell", "support_break",
                        f"跌破 20 日支撑位 {sup}（现价 {price}）", sup20=sup, price=price))
    return out


# —— 盘口：打单流入与五档挂单 ——

def _flow(v: View, cfg: SignalConfig) -> list[Signal]:
    inflow = v.flow.get("inflow_amount")
    if inflow is None:
        return []
    out = []
    if _hit(v, cfg, "order_flow_inflow") and inflow >= cfg.inflow_amount:
        out.append(_sig(v, "buy", "order_flow_inflow",
                        f"打单流入：近 {v.flow.get('window_min', 0):.0f} 分钟主动净买入 "
                        f"{inflow / 1e4:.0f} 万元", inflow_amount=round(inflow, 2),
                        net_shares=v.flow.get("net_shares")))
    if _hit(v, cfg, "order_flow_outflow") and inflow <= -cfg.inflow_amount:
        out.append(_sig(v, "sell", "order_flow_outflow",
                        f"打单流出：近 {v.flow.get('window_min', 0):.0f} 分钟主动净卖出 "
                        f"{-inflow / 1e4:.0f} 万元", inflow_amount=round(inflow, 2),
                        net_shares=v.flow.get("net_shares")))
    return out


def _book(v: View, cfg: SignalConfig) -> list[Signal]:
    """五档挂单：`bid_ratio` 是五档买量合计 / 卖量合计（引擎从 rt:quote 的五档现算）。

    只在**跨越**阈值时报：买/卖比 ≥ `book_ratio` 算托单，≤ 1/`book_ratio` 算压单。挂在阈值
    上不动的票每拍都重报，只会把告警刷成噪音。
    """
    ratio, prev = v.flow.get("bid_ratio"), v.flow.get("prev_bid_ratio")
    if not ratio or not prev:
        return []
    low = 1 / cfg.book_ratio
    out = []
    if _hit(v, cfg, "bid_stack") and prev < cfg.book_ratio <= ratio:
        out.append(_sig(v, "buy", "bid_stack",
                        f"买盘托单：五档买量是卖量的 {ratio:.1f} 倍（阈值 {cfg.book_ratio}）",
                        bid_ratio=round(ratio, 3)))
    if _hit(v, cfg, "ask_stack") and prev > low >= ratio:
        out.append(_sig(v, "sell", "ask_stack",
                        f"卖盘压单：五档卖量是买量的 {1 / ratio:.1f} 倍（阈值 {cfg.book_ratio}）",
                        bid_ratio=round(ratio, 3)))
    return out


# —— 涨跌停触板（成交性由风控判，这里只报「板到了」）——

def _limit(v: View, cfg: SignalConfig) -> list[Signal]:
    price = v.price
    if not price or v.quote.get("asset") == "fund":
        return []         # 基金不参涨跌停，口径与日线 lu 强制 False 一致
    out = []
    if _hit(v, cfg, "limit_up_touch") and limit_hit(price, v.quote.get("limit_up"), True):
        out.append(_sig(v, "buy", "limit_up_touch",
                        f"触及涨停价 {v.quote.get('limit_up')}：封板后买单排队，能不能买进由风控定",
                        limit_up=v.quote.get("limit_up")))
    if _hit(v, cfg, "limit_down_touch") and limit_hit(price, v.quote.get("limit_down"), False):
        out.append(_sig(v, "sell", "limit_down_touch",
                        f"触及跌停价 {v.quote.get('limit_down')}：封板后卖单排队，能不能卖出由风控定",
                        limit_down=v.quote.get("limit_down")))
    return out


# —— 基本面事件：当日公告/新闻拆出来的多空目标 ——

def _events(v: View, cfg: SignalConfig) -> list[Signal]:
    out = []
    for item in v.events or []:
        direction = item.get("direction")
        kind = {"bullish": "event_bullish", "bearish": "event_bearish"}.get(direction)
        if not kind or not _hit(v, cfg, kind):
            continue      # direction=watch 是「观察名单」不表态，不产信号
        category = item.get("category") or "事件"
        reason = (item.get("reason") or "").strip()
        out.append(_sig(v, "buy" if direction == "bullish" else "sell", kind,
                        f"{category}：{reason}"[:200] if reason else category,
                        event_category=category, event_score=item.get("score"),
                        event_priority=item.get("priority")))
    return out
