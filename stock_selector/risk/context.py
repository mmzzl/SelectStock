"""risk/context.py — 风控判定所需的一份输入：信号 + 行情口径 + 持仓现状。

策略层只产出「信号」（代码/方向/信号价/买点价），风控层要的远不止这些：成交性要看
交易所的涨跌停价、流动性要看日均成交额、仓位要看总资金和已有持仓。这些一律在这里
一次取齐，规则函数只吃 RiskContext，签名保持一致，才能被引擎按同一条流水线调用。

单位约定（全模块统一，别在规则里再换算）：
- 金额一律「元」，成交量「手」（1 手 = 100 股）；
- 市值「元」，但快照里的流通/总市值是「亿元」，进来时就换算掉；
- 滑点/冲击用「万分比 bp」，1bp = 0.01%。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

from . import snapshot as snap

logger = logging.getLogger(__name__)

YI = 1e8                    # 快照里流通市值单位是亿元
LOT = 100                   # A 股一手 100 股
ADV_WINDOW = 20             # 日均成交额窗口
MIN_ADV_ROWS = 5            # 少于 5 根不估（新股/长期停牌估出来没意义）


def _daily_amounts(df: pd.DataFrame) -> pd.Series:
    """日线单日成交额（元）。amount 自 2026-05-21 起断供为 0，退回 volume(手)×close×100。"""
    close = df["close"].astype(float)
    volume = df["volume"].astype(float)
    amount = df["amount"].astype(float) if "amount" in df.columns else None
    est = volume * close * LOT
    return est if amount is None else amount.where(amount > 0, est)


@dataclass
class RiskContext:
    code: str
    name: str = ""
    action: str = "buy"                  # buy | sell：风控判的是「这个方向能不能成交」
    signal_price: float | None = None    # 策略给的参考价（信号价/买点价）
    shares: int | None = None            # 拟下单股数；None = 让风控按仓位上限给建议
    position_value: float = 0.0          # 该标的已有持仓市值（元），买入时占用同一额度
    day: str = ""                        # 基准交易日 YYYY-MM-DD，决定读哪天的落盘快照
    kline: pd.DataFrame | None = None
    quote: dict = field(default_factory=dict)   # 行情口径（snapshot.get 的结果）
    allow_live: bool = True              # 回测里关掉：历史日期不能拿今天的实时行情判成交
    # 该票在**当日全市场**横截面里的百分位（0~100）。分母是全市场而不是它自己，
    # 所以只能由内核预扫后塞进来（见 risk/cross_section.py）。None = 本次没算，
    # 规则按「信息不够就不判」放行，不当成 0 分位。
    cross_pct: float | None = None
    # 下面三项都是「今天这一天账户的处境」，只有盘中链路给得出来（盘后内核留 None = 不判）。
    # 全部按「数据不可得就放行并留指标」处理：判不了不等于不准买，硬拦会把好信号静默吃掉。
    pool_rank: int | None = None         # 该票在当日盯的名单里的名次（0 起），买入名额用它
    bought_today: bool | None = None     # 今日已成交买入（T+1：今天买的今天卖不掉）
    cash_left: float | None = None       # 账户当日还剩下可用资金（元）

    @property
    def price(self) -> float | None:
        """成交价基准：优先信号价，其次行情最新价。两个都没有就没法判（规则会给「不可得」）。"""
        return self.signal_price if self.signal_price else self.quote.get("price")

    @property
    def is_fund(self) -> bool:
        return self.quote.get("asset") == "fund"

    @property
    def avg_amount(self) -> float | None:
        """近 ADV_WINDOW 日日均成交额（元）——流动性与冲击成本的基数。"""
        df = self.kline
        if df is None or df.empty:
            return None
        if not all(c in df.columns for c in ("close", "volume")):
            return None
        values = _daily_amounts(df.tail(ADV_WINDOW))
        # 停牌行是整行 0，混进均值会把流动性系统性低估，进而误杀复牌票
        values = values[values > 0]
        if len(values) < MIN_ADV_ROWS:
            return None
        return float(values.mean())

    @property
    def daily_vol_pct(self) -> float:
        """日涨幅标准差（百分点），冲击成本的波动项。缺数据给保守值而不是 0。"""
        df = self.kline
        if df is not None and "pct" in df.columns and len(df) >= MIN_ADV_ROWS:
            value = float(df["pct"].tail(ADV_WINDOW).std() or 0.0)
            if value > 0:
                return value
        return 2.0

    @property
    def float_mv_yuan(self) -> float | None:
        value = self.quote.get("float_mv")
        return value * YI if value else None


def build(code: str, action: str = "buy", *, name: str = "", signal_price: float | None = None,
          shares: int | None = None, position_value: float = 0.0, day: str = "",
          kline: pd.DataFrame | None = None, quote: dict | None = None,
          allow_live: bool = True, cross_pct: float | None = None,
          pool_rank: int | None = None, bought_today: bool | None = None,
          cash_left: float | None = None) -> RiskContext:
    """补齐行情口径后生成上下文；`quote` 显式传入时不再读快照（单测与盘中链路用）。"""
    if quote is None:
        quote = snap.get(code, day, allow_live=allow_live)
    return RiskContext(code=str(code).zfill(6), name=name, action=action,
                       signal_price=signal_price, shares=shares,
                       position_value=position_value, day=day, kline=kline,
                       quote=quote, allow_live=allow_live, cross_pct=cross_pct,
                       pool_rank=pool_rank, bought_today=bought_today,
                       cash_left=cash_left)
