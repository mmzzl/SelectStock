"""盘中计算层：分钟条还原、分钟指标与交叉、三类信号、引擎一轮（含风控与落盘）、告警排版。

时钟与 redis 全部注入（假客户端见 tests/redis_fake.py）：「现在是不是盘中」「这条 tick 是不是
旧到只该拿来补条」都取决于时间，用真时钟的用例会在收盘后集体翻脸。
"""

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from redis_fake import FakeRedis                                        # noqa: E402
from stock_selector.intraday import bars as minute_bars                  # noqa: E402
from stock_selector.intraday import engine as engine_mod                 # noqa: E402
from stock_selector.intraday import indicators, signals                  # noqa: E402
from stock_selector.intraday.bars import BarBuilder                      # noqa: E402
from stock_selector.intraday.engine import EngineConfig, IntradayEngine, book_ratio  # noqa: E402
from stock_selector.intraday.indicators import Snapshot                  # noqa: E402
from stock_selector.intraday.signals import SignalConfig, View, evaluate  # noqa: E402
from stock_selector.intraday.store import append_parquet, doc_id, row    # noqa: E402
from stock_selector.realtime import bus                                  # noqa: E402
from stock_selector.realtime.pool import MonitorPool                     # noqa: E402
from stock_selector.risk import RiskEngine                                # noqa: E402

# 2026-10-12 是周一，10:00 在早盘时段内
NOW = datetime(2026, 10, 12, 10, 0, 0)
TODAY = "20261012"
DAY = "2026-10-12"
TS = NOW.timestamp()
MINUTE = minute_bars.minute_of(TS)

RISK = {"enabled": True, "capital": 200000, "max_position_pct": 0.2, "min_avg_amount": 3e7,
        "max_slippage_bp": 1e9, "max_drift_bp": 1e9, "ml": {"enabled": False}}


def tick(code="600000", ts=TS, **over):
    base = {"code": code, "ts": ts, "price": 10.0, "pct": 1.0, "volume": 1000.0,
            "amount": 1e7, "turnover_pct": 0.5, "vol_ratio": 1.0, "limit_up": 11.0,
            "limit_down": 9.0, "outer_volume": 600.0, "inner_volume": 400.0,
            "quote_time": f"{TODAY}100000"}
    base.update(over)
    return base


def quote(code="600000", price=10.0, **over):
    base = {"code": code, "name": "浦发银行", "price": price, "pct": 1.0, "volume": 1000.0,
            "amount": 1e7, "turnover_pct": 0.5, "vol_ratio": 1.0, "limit_up": 11.0,
            "limit_down": 9.0, "bids": [[9.99, 100], [9.98, 50]], "asks": [[10.01, 60]],
            "quote_time": f"{TODAY}100000"}
    base.update(over)
    return base


def kline(rows=70, price=10.0, volume=50000.0, res20=None, sup20=None):
    """日线宽表的最小形状：风控要 close/volume/amount/pct/lu，信号要 res20/sup20。

    日均成交额刻意给到 5e7 元（5 万手 × 10 元 × 100），否则会被流动性规则误杀。
    """
    dates = pd.date_range("2026-01-01", periods=rows, freq="D")
    return pd.DataFrame({
        "date": dates, "close": price, "high": price * 1.01, "low": price * 0.99,
        "volume": volume, "amount": volume * price * 100, "pct": 0.5, "lu": False,
        "res20": res20 if res20 is not None else price * 1.05,
        "sup20": sup20 if sup20 is not None else price * 0.95})


def snap(current, previous):
    """手搓一份指标快照：交叉类用例不必造 60 根条，判据本身要单独能测。"""
    return Snapshot(bars=70, minute=MINUTE, current=current, previous=previous)


@pytest.fixture
def client():
    return FakeRedis()


@pytest.fixture
def out_dir(tmp_path):
    return tmp_path / "intraday_decisions"


def feed(client, code="600000", **over):
    """快照与 tick 写同一组数：引擎优先读 `rt:quote`，两边不一致时信号按快照判、tick 白费。"""
    bus.put_quotes(client, {code: quote(code=code, **over)})
    bus.emit_ticks(client, [tick(code=code, **over)])


def make_engine(client, pool=("600000",), *, cfg=None, signal_cfg=None, frame=None,
                events_map=None, out_dir=None, risk=None, **over):
    """假 redis + 固定时钟 + 内存日线：一轮 `drain()` 的全部外部依赖都在参数里。"""
    inject = dict(now=lambda: NOW, sleeper=lambda s: None, clock=lambda: 0.0,
                  kline_loader=lambda code: frame if frame is not None else kline(),
                  event_loader=lambda day, codes: events_map or {})
    inject.update(over)
    return IntradayEngine(client, list(pool), risk or RiskEngine(RISK),
                          cfg=cfg or EngineConfig(),
                          signal_cfg=signal_cfg or SignalConfig(min_bars=60),
                          out_dir=out_dir, **inject)


# —— bars：累计量 → 分钟条 ——

def test_first_tick_only_sets_baseline():
    b = BarBuilder()
    assert b.apply("600000", tick(volume=1000.0)) is False
    assert b.current("600000")["volume"] == 0.0        # 不知道之前成交多少，不编数字
    assert b.closed("600000") == []


def test_same_minute_merges_deltas():
    b = BarBuilder()
    b.apply("600000", tick(volume=1000.0, amount=1e7, price=10.0))
    # 增量量与增量额各取各的差（额不是量×价算出来的），50 手在这里对应 5.1 万元
    b.apply("600000", tick(volume=1050.0, amount=1e7 + 51000.0, price=10.2,
                           ts=TS + 5))
    cur = b.current("600000")
    assert cur["volume"] == pytest.approx(50.0)
    assert cur["amount"] == pytest.approx(51000.0)
    assert cur["high"] == 10.2 and cur["close"] == 10.2 and cur["open"] == 10.0


def test_rolling_minute_seals_previous_bar():
    b = BarBuilder()
    b.apply("600000", tick(volume=1000.0))
    b.apply("600000", tick(volume=1100.0, price=10.1, ts=TS + 60))
    sealed = b.closed("600000")
    assert len(sealed) == 1 and sealed[0]["volume"] == 0.0
    assert sealed[0]["minute"] == MINUTE and sealed[0]["ts"] == MINUTE * 60


def test_cumulative_decrease_rebaselines_instead_of_negative_volume():
    b = BarBuilder()
    b.apply("600000", tick(volume=1000.0))
    b.apply("600000", tick(volume=1100.0, ts=TS + 60))        # 收口第一根
    b.apply("600000", tick(volume=50.0, ts=TS + 120))         # 累计值倒退 = 基线不可信
    assert b.current("600000")["volume"] == 0.0
    assert all(bar["volume"] >= 0 for bar in b.closed("600000"))


def test_day_change_forgets_yesterday_bars():
    b = BarBuilder()
    b.apply("600000", tick(volume=1000.0))
    b.apply("600000", tick(volume=1100.0, ts=TS + 60))
    assert len(b.closed("600000")) == 1
    b.apply("600000", tick(volume=20.0, quote_time="20261013100000", ts=TS + 86460))
    assert b.closed("600000") == []                          # 昨天的条不能混进今天的均线
    assert b.current("600000")["volume"] == 0.0


def test_tick_without_ts_or_price_is_dropped():
    b = BarBuilder()
    assert b.apply("600000", tick(ts=None)) is False
    assert b.apply("600000", tick(price=0)) is False
    assert b.current("600000") is None


def test_capacity_limits_bars():
    b = BarBuilder(capacity=2)
    for step in range(5):
        b.apply("600000", tick(ts=TS + step * 60, volume=1000.0 + step))
    closed = b.closed("600000")
    assert len(closed) == 2 and closed[0]["minute"] == MINUTE + 2


# —— indicators：预热与交叉 ——

def rising_bars(count=70, start=10.0):
    out = []
    for i in range(count):
        price = round(start + i * 0.02, 3)
        out.append({"minute": MINUTE + i, "ts": (MINUTE + i) * 60, "open": price,
                    "high": price * 1.001, "low": price * 0.999, "close": price,
                    "volume": 100.0 + i, "amount": (100.0 + i) * price * 100})
    return out


def test_compute_returns_none_before_warmup():
    assert indicators.compute(rising_bars(59)) is None         # 不给半温的均线
    assert indicators.compute(rising_bars(60)) is not None


def test_compute_values_and_bar_pct():
    s = indicators.compute(rising_bars(70))
    assert s.bars == 70 and s.minute == MINUTE + 69
    closes = [b["close"] for b in rising_bars(70)]
    assert s.current["ma5"] == pytest.approx(sum(closes[-5:]) / 5, abs=1e-4)
    assert s.current["ma60"] is not None
    assert s.current["close"] == pytest.approx(closes[-1])
    assert s.current["bar_pct"] == pytest.approx(
        (closes[-1] / closes[-2] - 1) * 100, abs=1e-4)
    assert s.current["macd_dif"] > 0                      # 匀速上升段 dif 为正（dea 已跟上，交叉要收口条判）


def test_crossed_up_and_down():
    up = snap({"ma5": 10.2, "ma10": 10.1, "close": 10.2}, {"ma5": 10.0, "ma10": 10.05,
                                                            "close": 10.0})
    assert indicators.crossed_up(up, "ma5", "ma10") is True
    assert indicators.crossed_down(up, "ma5", "ma10") is False
    missing = snap({"ma5": None, "ma10": 10.0, "close": 1}, {"ma5": 9.0, "ma10": 9.0,
                                                              "close": 1})
    assert indicators.crossed_up(missing, "ma5", "ma10") is False   # 缺数不猜成交叉


def test_level_up_reads_close_versus_band():
    s = snap({"close": 10.6, "boll_upper": 10.5}, {"close": 10.4, "boll_upper": 10.5})
    assert indicators.level_up(s) is True
    assert indicators.level_down(s) is False


# —— signals ——

def test_limit_up_touch_is_a_buy_signal_that_risk_will_reject():
    v = View(code="600000", quote=quote(price=11.0, limit_up=11.0), minute=MINUTE)
    out = evaluate(v, SignalConfig())
    kinds = {s.kind: s for s in out}
    assert "limit_up_touch" in kinds
    assert kinds["limit_up_touch"].action == "buy"       # 表态是「想买」，能不能买由风控定
    assert "涨停" in kinds["limit_up_touch"].reason      # 「买不进」由风控说，信号只报触板


def test_fund_has_no_limit_signal():
    v = View(code="510300", quote=quote(code="510300", price=4.824, limit_up=4.824,
                                        asset="fund"))
    assert [s.kind for s in evaluate(v, SignalConfig())] == []


def test_ma_and_macd_crosses_come_from_snapshot():
    v = View(code="600000", quote=quote(), snap=snap(
        {"close": 10.2, "ma5": 10.2, "ma10": 10.1, "ma20": 10.0, "macd_dif": 0.02,
         "macd_dea": 0.01, "macd_hist": 0.01, "rsi14": 55.0, "boll_upper": 10.5,
         "boll_lower": 9.5, "boll_mid": 10.0, "vol_ma5": 100.0, "volume": 100.0},
        {"close": 10.0, "ma5": 10.0, "ma10": 10.05, "ma20": 10.0, "macd_dif": 0.0,
         "macd_dea": 0.01, "macd_hist": -0.01, "rsi14": 50.0, "boll_upper": 10.4,
         "boll_lower": 9.6, "boll_mid": 10.0, "vol_ma5": 100.0, "volume": 100.0}))
    kinds = {s.kind for s in evaluate(v, SignalConfig())}
    assert {"ma_golden_cross", "macd_golden_cross"} <= kinds


def test_dead_cross_and_rsi_bands():
    v = View(code="600000", quote=quote(), snap=snap(
        {"close": 9.8, "ma5": 9.8, "ma10": 9.9, "ma20": 10.0, "macd_dif": -0.01,
         "macd_dea": 0.0, "rsi14": 72.0, "vol_ma5": 100.0, "volume": 100.0},
        {"close": 10.0, "ma5": 10.0, "ma10": 9.95, "ma20": 10.1, "macd_dif": 0.01,
         "macd_dea": 0.0, "rsi14": 68.0, "vol_ma5": 100.0, "volume": 100.0}))
    got = {s.kind: s for s in evaluate(v, SignalConfig())}
    assert {"ma_dead_cross", "macd_dead_cross", "rsi_overbought"} <= set(got)
    assert got["rsi_overbought"].action == "sell"


def test_volume_surge_drop_needs_both_blast_and_fall():
    def view(volume, bar_pct):
        return View(code="600000", quote=quote(), snap=snap(
            {"close": 10.0, "volume": volume, "vol_ma5": 100.0, "bar_pct": bar_pct},
            {"close": 10.1, "volume": 100.0, "vol_ma5": 100.0, "bar_pct": 0.0}))

    assert {s.kind for s in evaluate(view(300.0, -1.4), SignalConfig())} == \
        {"volume_surge_drop"}
    assert evaluate(view(300.0, -0.2), SignalConfig()) == []      # 放量但没跌够
    assert evaluate(view(120.0, -1.4), SignalConfig()) == []      # 跌够但没放量


def test_day_capitulation_and_vol_ratio_and_turnover():
    v = View(code="600000", quote=quote(pct=-6.0, vol_ratio=2.5, turnover_pct=12.0))
    got = {s.kind: s for s in evaluate(v, SignalConfig())}
    assert got["volume_surge_drop"].action == "sell"
    assert "turnover_hot" in got and got["turnover_hot"].action == "sell"
    assert "vol_ratio_surge" not in got                            # 跌的时候量比不算上攻


def test_breakout_needs_previous_price_below_the_level():
    day = {"res20": 10.5, "sup20": 9.5}
    up = View(code="600000", quote=quote(price=10.60), day=day, last_price=10.48)
    assert {s.kind for s in evaluate(up, SignalConfig())} == {"breakout_resistance"}
    hovering = View(code="600000", quote=quote(price=10.51), day=day, last_price=10.50)
    assert evaluate(hovering, SignalConfig()) == []                # 确认幅度不够
    dn = View(code="600000", quote=quote(price=9.40), day=day, last_price=9.55)
    assert {s.kind for s in evaluate(dn, SignalConfig())} == {"support_break"}


def test_order_flow_inflow_and_outflow():
    cfg = SignalConfig(inflow_amount=5e6)
    v = View(code="600000", quote=quote(pct=1.0),
             flow={"inflow_amount": 8e6, "window_min": 5})
    assert {s.kind for s in evaluate(v, cfg)} == {"order_flow_inflow"}
    v = View(code="600000", quote=quote(pct=-1.0),
             flow={"inflow_amount": -8e6, "window_min": 5})
    assert {s.kind for s in evaluate(v, cfg)} == {"order_flow_outflow"}


def test_book_stack_fires_only_on_crossing():
    cfg = SignalConfig(book_ratio=2.0)
    crossing = View(code="600000", quote=quote(),
                    flow={"bid_ratio": 3.0, "prev_bid_ratio": 1.5})
    assert {s.kind for s in evaluate(crossing, cfg)} == {"bid_stack"}
    already = View(code="600000", quote=quote(),
                   flow={"bid_ratio": 3.0, "prev_bid_ratio": 3.0})
    assert evaluate(already, cfg) == []                            # 一直在高位不重复报
    pressure = View(code="600000", quote=quote(),
                    flow={"bid_ratio": 0.3, "prev_bid_ratio": 0.8})
    assert {s.kind for s in evaluate(pressure, cfg)} == {"ask_stack"}


def test_events_are_signals_with_the_category_and_reason():
    v = View(code="600000", quote=quote(), events=[
        {"direction": "bullish", "category": "业绩", "reason": "预告净利润同比+80%",
         "score": 8},
        {"direction": "bearish", "category": "减持", "reason": "大股东拟减持不超 2%",
         "score": 6},
        {"direction": "watch", "category": "龙虎榜", "reason": "游资买入"},
    ])
    got = {s.kind: s for s in evaluate(v, SignalConfig())}
    assert set(got) == {"event_bullish", "event_bearish"}          # watch 不表态就不产信号
    assert got["event_bullish"].action == "buy"
    assert "业绩" in got["event_bullish"].reason and "+80%" in got["event_bullish"].reason
    assert got["event_bearish"].metrics["event_category"] == "减持"


def test_kinds_whitelist_filters():
    v = View(code="600000", quote=quote(price=11.0, limit_up=11.0), minute=MINUTE)
    assert evaluate(v, SignalConfig(kinds=("turnover_hot",))) == []
    assert {s.kind for s in evaluate(v, SignalConfig(kinds=("limit_up_touch",)))} == \
        {"limit_up_touch"}


def test_signal_config_ignores_unknown_keys():
    cfg = SignalConfig.from_dict({"book_ratio": 3.0, "nonsense": 1})
    assert cfg.book_ratio == 3.0
    assert cfg.kinds is None


# —— 引擎一轮 ——

def test_drain_builds_bars_metrics_and_decision(client, out_dir):
    bus.put_quotes(client, {"600000": quote(price=11.0, limit_up=11.0)})
    bus.emit_ticks(client, [tick(price=11.0, limit_up=11.0)])
    engine = make_engine(client, out_dir=out_dir)

    stats = engine.drain()
    assert stats["ticks"] == 1 and stats["changed"] == 1 and stats["decisions"] == 1
    assert stats["blocked"] == 1                          # 涨停买不进，风控必须拦
    assert client.xlen(bus.KEY_DECISIONS) == 1

    fields = client.xrange(bus.KEY_DECISIONS)[0][1]
    assert fields["kind"] == "limit_up_touch" and fields["verdict"] == "block"
    assert fields["blocked_by"] == "tradability"
    assert fields["pushed"] == "0"                        # 拦下的不推送，但要留痕
    assert "涨停" in fields["risk_reason"]

    assert len(engine.bars.closed("600000")) + (1 if engine.bars.current("600000") else 0) == 1
    assert bus.last_heartbeat(client, key=bus.KEY_ENGINE_HEARTBEAT)["status"] == "ok"
    assert Path(out_dir / f"{DAY}.parquet").is_file()


def test_drain_writes_indicators_only_when_a_bar_seals(client, out_dir):
    engine = make_engine(client, out_dir=out_dir,
                         signal_cfg=SignalConfig(min_bars=2))
    bus.put_quotes(client, {"600000": quote()})
    bus.emit_ticks(client, [tick(ts=TS), tick(ts=TS + 60, price=10.1, volume=1100.0)])
    engine.drain()
    assert client.hgetall(bus.metric_key("600000")) == {}     # 还没收口，不写指标
    assert engine.bars.current("600000")["minute"] == MINUTE + 1


def test_metrics_land_in_redis_after_warmup(client, out_dir):
    """预热够的分钟条 → 指标进 `rt:metric:{code}`（「redis 存实时指标」那一格）。"""
    engine = make_engine(client, out_dir=out_dir, signal_cfg=SignalConfig(min_bars=3))
    bus.put_quotes(client, {"600000": quote()})
    bus.emit_ticks(client, [tick(ts=TS - step * 60, volume=1000.0 + step * 100,
                                 price=10.0 + step * 0.01)
                            for step in (5, 4, 3, 2, 1, 0)])
    engine.drain()
    metrics = bus.get_metrics(client, "600000")
    assert int(metrics["bars"]) == 5                     # 前五根收口，最后一根还在长
    assert metrics["ma5"] and metrics["minute"]
    assert "ma60" not in metrics                         # 算不出来的指标不写，而不是写空值


def test_replayed_ticks_build_bars_but_produce_no_signals(client, out_dir):
    bus.put_quotes(client, {"600000": quote(price=11.0, limit_up=11.0)})
    bus.emit_ticks(client, [tick(price=11.0, limit_up=11.0, ts=TS - 900)])
    engine = make_engine(client, out_dir=out_dir)
    stats = engine.drain()
    assert stats["replayed"] == 1 and stats["decisions"] == 0
    assert engine.bars.current("600000") is not None     # 条要补上，指标才有预热的数据


def test_cooldown_suppresses_the_same_signal(client, out_dir):
    """同一信号在冷却窗口内不再重复出决策：行情一直在动，告警不能每五秒刷一条。

    这是 `once_per_day` 关掉后的口径（`--no-daily-dedupe`、或去重表写不进 redis 时的退路）。
    每轮都要新发一条 tick——决策流是按 tick 驱动的，游标划过就没有输入了。
    """
    bus.put_quotes(client, {"600000": quote(price=11.0, limit_up=11.0)})
    engine = make_engine(client, out_dir=out_dir,
                         cfg=EngineConfig(cooldown=300, once_per_day=False))
    decisions = []
    for step in range(1, 4):
        bus.emit_ticks(client, [tick(price=11.0, limit_up=11.0, ts=TS + step)])
        decisions.append(engine.drain()["decisions"])
    assert decisions == [1, 0, 0]
    engine._emitted.clear()                               # 冷却过期后同一信号还能再报
    bus.emit_ticks(client, [tick(price=11.0, limit_up=11.0, ts=TS + 4)])
    assert engine.drain()["decisions"] == 1


def test_same_signal_emits_only_once_per_day(client, out_dir):
    """默认口径：同票同信号**当天只报一次**，冷却时间早过了也不再报。"""
    bus.put_quotes(client, {"600000": quote(price=11.0, limit_up=11.0)})
    engine = make_engine(client, out_dir=out_dir, cfg=EngineConfig(cooldown=0))
    decisions = []
    for step in range(1, 4):
        bus.emit_ticks(client, [tick(price=11.0, limit_up=11.0, ts=TS + step)])
        decisions.append(engine.drain()["decisions"])
    assert decisions == [1, 0, 0]
    assert client.sets[bus.emitted_key(DAY)] == {"600000|limit_up_touch"}


def test_daily_dedupe_survives_an_engine_restart(client, out_dir):
    """重启不能把上午报过的信号再报一遍——去重表在 redis 里，不在引擎进程里。"""
    bus.put_quotes(client, {"600000": quote(price=11.0, limit_up=11.0)})
    bus.emit_ticks(client, [tick(price=11.0, limit_up=11.0, ts=TS)])
    assert make_engine(client, out_dir=out_dir).drain()["decisions"] == 1
    bus.emit_ticks(client, [tick(price=11.0, limit_up=11.0, ts=TS + 60)])
    restarted = make_engine(client, out_dir=out_dir)      # 全新实例：_emitted 是空的
    assert not restarted._emitted
    assert restarted.drain()["decisions"] == 0


def test_next_day_frees_the_same_signal(client, out_dir):
    """去重表按日期分键：跨天不需要清理，第二天同一信号照样能报。"""
    bus.put_quotes(client, {"600000": quote(price=11.0, limit_up=11.0)})
    clock = {"value": NOW}
    engine = make_engine(client, out_dir=out_dir, now=lambda: clock["value"])
    bus.emit_ticks(client, [tick(price=11.0, limit_up=11.0, ts=TS)])
    assert engine.drain()["decisions"] == 1
    clock["value"] = datetime(2026, 10, 13, 10, 0, 0)
    # 换日后游标归零，昨天那条 tick 会被重读，但它已旧过回放窗口 → 只补条、不产信号
    bus.emit_ticks(client, [tick(price=11.0, limit_up=11.0, ts=clock["value"].timestamp())])
    assert engine.drain()["decisions"] == 1
    assert client.sets[bus.emitted_key("2026-10-13")] == {"600000|limit_up_touch"}


def test_dedupe_falls_back_to_cooldown_when_bus_write_fails(client, out_dir):
    """中间件抖动时不能把整条链路卡住：退化成 `cooldown` 窗口，宁多报也不漏报。"""
    bus.put_quotes(client, {"600000": quote(price=11.0, limit_up=11.0)})

    def boom(key, *values):
        raise ConnectionError("redis 连不上")

    client.sadd = boom
    engine = make_engine(client, out_dir=out_dir, cfg=EngineConfig(cooldown=0))
    counts = []
    for step in range(1, 3):
        bus.emit_ticks(client, [tick(price=11.0, limit_up=11.0, ts=TS + step)])
        counts.append(engine.drain()["decisions"])
    assert counts == [1, 1]                              # 冷却 0 秒 = 退路下同一信号仍可再报
    assert client.sets == {}                             # 一次都没写进去，也没留半截状态


def test_shipped_config_enables_daily_dedupe():
    """出厂配置必须是「当天只报一次」，否则线上跑的还是 5 分钟重复报的旧口径。"""
    import yaml

    raw = yaml.safe_load((ROOT / "stock_selector" / "config" / "settings.yaml").read_text("utf-8"))
    cfg = EngineConfig.from_dict(raw["intraday"])
    assert cfg.once_per_day is True and cfg.cooldown == 300


def test_max_eval_bounds_the_indicator_work_per_round(client, out_dir):
    codes = ["600000", "000001"]
    bus.put_quotes(client, {c: quote(code=c) for c in codes})
    bus.emit_ticks(client, [tick(code=c) for c in codes])
    engine = make_engine(client, codes, out_dir=out_dir, cfg=EngineConfig(max_eval=1))
    stats = engine.drain()
    assert stats["changed"] == 2 and stats["evaluated"] == 1


def test_allowed_decision_carries_shares_and_fill_price(client, out_dir):
    """放行侧：决策必须带建议股数/预估成交价/滑点，消息与复盘都读这几个数。"""
    feed(client, price=10.0, limit_up=11.0, vol_ratio=4.0, pct=2.0)
    engine = make_engine(client, out_dir=out_dir)
    rows = engine.drain()
    assert rows["decisions"] >= 1
    fields = dict(client.xrange(bus.KEY_DECISIONS)[-1][1])
    assert fields["verdict"] in ("allow", "adjust")
    assert int(fields["shares"]) > 0 and float(fields["order_amount"]) <= 40000 + 1
    assert fields["pushed"] == "1"
    assert float(fields["slippage_bp"]) >= 0


def test_position_cap_shrinks_when_holding_already(client, out_dir):
    feed(client, vol_ratio=4.0, pct=2.0)
    plain = make_engine(client, out_dir=out_dir)
    plain.drain()
    shares = int(client.xrange(bus.KEY_DECISIONS)[-1][1]["shares"])

    client2 = FakeRedis()
    feed(client2, vol_ratio=4.0, pct=2.0)
    held = make_engine(client2, out_dir=out_dir,
                       cfg=EngineConfig(held={"600000": 30000.0}))
    held.drain()
    assert int(client2.xrange(bus.KEY_DECISIONS)[-1][1]["shares"]) < shares


def test_events_become_decisions_with_risk_verdict(client, out_dir):
    bus.put_quotes(client, {"600000": quote()})
    bus.emit_ticks(client, [tick()])
    engine = make_engine(client, out_dir=out_dir, events_map={"600000": [
        {"direction": "bullish", "category": "业绩", "reason": "预告净利+80%"}]})
    engine.drain()
    kinds = {fields["kind"] for _, fields in client.xrange(bus.KEY_DECISIONS)}
    assert "event_bullish" in kinds


def test_thin_liquidity_signal_is_blocked(client, out_dir):
    feed(client, vol_ratio=4.0, pct=2.0)
    engine = make_engine(client, out_dir=out_dir, frame=kline(volume=1000.0))   # 日均 1e6 元
    assert engine.drain()["blocked"] >= 1
    fields = dict(client.xrange(bus.KEY_DECISIONS)[-1][1])
    assert fields["blocked_by"] == "liquidity" and fields["verdict"] == "block"


def test_missing_kline_does_not_stop_the_round(client, out_dir):
    feed(client, vol_ratio=4.0, pct=2.0)
    engine = make_engine(client, out_dir=out_dir)
    engine._kline_loader = lambda code: None
    stats = engine.drain()
    assert stats["decisions"] >= 1                       # 没有日线就少两条规则，不是整轮作废
    fields = dict(client.xrange(bus.KEY_DECISIONS)[-1][1])
    assert fields["verdict"] in ("allow", "adjust")


def test_day_rollover_resets_state(client, out_dir):
    clock = {"value": NOW}
    engine = make_engine(client, out_dir=out_dir, now=lambda: clock["value"])
    feed(client, price=11.0, limit_up=11.0)
    engine.drain()
    assert engine.cursor != "0-0" and engine._emitted
    clock["value"] = datetime(2026, 10, 13, 10, 0, 0)
    stats = engine.drain()
    assert engine.day == "2026-10-13"
    assert engine.bars.closed("600000") == []             # 昨天的分钟条不带进今天
    assert engine._emitted == {} and engine._last_price == {}
    # 那条 tick 现在属于「昨天 10 点」，比现在旧过回放窗口 → 只补条、不产信号
    assert stats["replayed"] == 1 and stats["decisions"] == 0


def test_quote_falls_back_to_tick_when_hash_missing(client, out_dir):
    """rt:quote 里没有这只票（hash 写失败等边角）时用 tick 拼最小快照，滑点走保守下限。"""
    bus.emit_ticks(client, [tick(vol_ratio=4.0, pct=2.0)])
    engine = make_engine(client, out_dir=out_dir)
    assert engine.drain()["decisions"] >= 1
    fields = dict(client.xrange(bus.KEY_DECISIONS)[-1][1])
    assert json.loads(fields["metrics"])["source"] != ""


def test_run_sleeps_outside_trading_window(client):
    calls = []
    engine = make_engine(client, now=lambda: datetime(2026, 10, 12, 16, 0, 0),
                         sleeper=calls.append)
    engine.run(rounds=1)
    assert calls and calls[0] > 0                        # 收盘后睡到明天开盘
    assert client.strings == {}                          # 一轮都没跑，不写心跳


def test_engine_config_ignores_unknown_keys():
    cfg = EngineConfig.from_dict({"interval": 3, "nonsense": 1})
    assert cfg.interval == 3 and cfg.cooldown == 300


# —— 池：只盯「选股命中 + 持仓」，池外的 tick 直接丢 ——

def test_ticks_outside_pool_are_not_built_or_evaluated(client, out_dir):
    """上游轮询可能还在发全市场；给谁算指标由引擎的池决定，否则 max_eval 全花在没人看的票上。"""
    feed(client, code="600000")
    feed(client, code="000001")
    engine = make_engine(client, pool=("600000",), out_dir=out_dir)
    stats = engine.drain()
    assert stats["skipped"] == 1 and stats["changed"] == 1
    assert engine.bars.current("000001") is None          # 池外的票连分钟条都不建


def test_empty_pool_means_no_filtering(client, out_dir):
    """空池是「没给池口径」而不是「一只都不看」：不能因为参数漏了就静默停掉全部判定。"""
    feed(client, code="600000")
    feed(client, code="000001")
    engine = make_engine(client, pool=(), out_dir=out_dir)
    stats = engine.drain()
    assert stats["skipped"] == 0 and stats["changed"] == 2


def test_day_rollover_refreshes_pool_and_held(client, out_dir):
    """换日重取：第二天盯的是前一天盘后选出来的名单，持仓市值也要跟着换成新的已用额度。"""
    clock = {"value": NOW}
    plan = MonitorPool(codes=["000001", "002855"], held={"002855": 5104.0},
                       generated_at=clock["value"])
    engine = make_engine(client, pool=("600000",), out_dir=out_dir,
                         now=lambda: clock["value"], cfg=EngineConfig(held={"600000": 999.0}),
                         pool_loader=lambda: plan)
    engine.drain()
    assert engine.pool == ["600000"] and engine.cfg.held == {"600000": 999.0}
    clock["value"] = datetime(2026, 10, 13, 10, 0, 0)
    engine.drain()
    assert engine.pool == ["000001", "002855"]
    assert engine._pool_set == {"000001", "002855"}
    assert engine.cfg.held == {"002855": 5104.0}


def test_pool_refresh_failure_keeps_previous_batch(client, out_dir, caplog):
    """读库失败沿用上一批：宁可盯一份可能过期的名单，也不要一条判定都不做。"""
    clock = {"value": NOW}

    def boom():
        raise RuntimeError("app.db 读不到")

    engine = make_engine(client, pool=("600000",), out_dir=out_dir,
                         now=lambda: clock["value"], pool_loader=boom)
    clock["value"] = datetime(2026, 10, 13, 10, 0, 0)
    with caplog.at_level("WARNING"):
        engine.drain()
    assert engine.pool == ["600000"] and "沿用上一批" in caplog.text


def test_pool_refresh_empty_keeps_previous_batch(client, out_dir, caplog):
    clock = {"value": NOW}
    engine = make_engine(client, pool=("600000",), out_dir=out_dir,
                         now=lambda: clock["value"],
                         pool_loader=lambda: MonitorPool(codes=[]))
    clock["value"] = datetime(2026, 10, 13, 10, 0, 0)
    with caplog.at_level("WARNING"):
        engine.drain()
    assert engine.pool == ["600000"] and "刷新为空" in caplog.text


# —— book_ratio / normalize ——

def test_book_ratio_uses_all_five_levels_and_caps_one_sided_book():
    assert book_ratio({"bids": [[1, 100], [2, 100]], "asks": [[3, 100]]}) == 2.0
    assert book_ratio({"bids": [[1, 100]], "asks": []}) == engine_mod.MAX_BOOK_RATIO
    assert book_ratio({"bids": [], "asks": [[3, 100]]}) == 0.0
    assert book_ratio({"bids": [], "asks": []}) is None      # 判不了就不判，不猜成中性


def test_normalize_adds_limit_flags_for_risk():
    from stock_selector.risk.snapshot import normalize

    out = normalize("600000", quote(price=11.0, limit_up=11.0, limit_down=9.0))
    assert out["at_limit_up"] is True and out["at_limit_down"] is False
    assert out["asset"] == "stock" and out["source"] == "live"
    assert normalize("510300", quote())["asset"] == "fund"


# —— store ——

def test_row_flattens_signal_and_decision():
    signal = signals.Signal(code="600000", action="buy", kind="limit_up_touch", price=11.0,
                            reason="触及涨停", name="浦发银行", minute=MINUTE,
                            metrics={"limit_up": 11.0})
    decision = RiskEngine(RISK).review_signal("600000", "buy", signal_price=11.0,
                                              quote=normalize_quote(quote(price=11.0)))
    item = row(signal, decision, ts=TS, day=DAY, pushed=False)
    assert item["verdict"] == "block" and item["blocked_by"] == "tradability"
    assert item["pushed"] == 0 and item["code"] == "600000"
    assert item["metrics"]["limit_up"] == 11.0             # 信号指标并进同一列，复盘只读一行


def normalize_quote(q):
    from stock_selector.risk.snapshot import normalize

    return normalize(q["code"], q)


def test_append_parquet_is_idempotent_per_code_kind_minute(out_dir):
    signal = signals.Signal(code="600000", action="buy", kind="x", price=10.0, reason="r",
                            minute=MINUTE)
    decision = RiskEngine(RISK).review_signal("600000", "buy", signal_price=10.0,
                                              quote=normalize_quote(quote()))
    first = row(signal, decision, ts=TS, day=DAY, pushed=True)
    append_parquet([first], DAY, out_dir)
    again = dict(first, signal_reason="r2", ts=TS + 5)
    append_parquet([again], DAY, out_dir)
    frame = pd.read_parquet(out_dir / f"{DAY}.parquet")
    assert len(frame) == 1 and frame.iloc[0]["signal_reason"] == "r2"
    assert json.loads(frame.iloc[0]["metrics"]) is not None


def test_append_parquet_merges_different_minutes(out_dir):
    decision = RiskEngine(RISK).review_signal("600000", "buy", signal_price=10.0,
                                              quote=normalize_quote(quote()))
    a = row(signals.Signal(code="600000", action="buy", kind="x", price=10.0, reason="r",
                           minute=MINUTE), decision, ts=TS, day=DAY, pushed=True)
    b = row(signals.Signal(code="000001", action="sell", kind="y", price=9.0, reason="r",
                           minute=MINUTE + 1), decision, ts=TS, day=DAY, pushed=True)
    append_parquet([a], DAY, out_dir)
    append_parquet([b], DAY, out_dir)
    assert len(pd.read_parquet(out_dir / f"{DAY}.parquet")) == 2


def test_blocked_round_keeps_decision_columns_float(out_dir):
    """整轮都被拦时 shares/成交价/滑点全是 None：不固定成 float 就是 object 列，
    跟已有文件的 float64 列 concat 会报 FutureWarning，同一列的 dtype 还随日期飘。"""
    import warnings

    passed = row(signals.Signal(code="600000", action="buy", kind="x", price=10.0,
                                 reason="r", minute=MINUTE),
                 RiskEngine(RISK).review_signal("600000", "buy", signal_price=10.0,
                                                quote=normalize_quote(quote())),
                 ts=TS, day=DAY, pushed=True)
    append_parquet([passed], DAY, out_dir)

    tight = quote(code="000001", price=11.0, limit_up=11.0)   # 已涨停 → 买单排队买不进，本轮只剩拦截
    blocked = row(signals.Signal(code="000001", action="buy", kind="y", price=11.0,
                                 reason="r", minute=MINUTE + 1),
                  RiskEngine(RISK).review_signal("000001", "buy", signal_price=11.0,
                                                 quote=normalize_quote(tight)),
                  ts=TS, day=DAY, pushed=False)
    assert blocked["verdict"] == "block" and blocked["shares"] is None
    with warnings.catch_warnings():
        warnings.simplefilter("error", FutureWarning)
        append_parquet([blocked], DAY, out_dir)

    frame = pd.read_parquet(out_dir / f"{DAY}.parquet")
    assert len(frame) == 2
    assert frame["shares"].dtype == "float64"
    assert frame["fill_price"].dtype == "float64"


def test_doc_id_is_stable_per_key():
    item = {"day": DAY, "code": "600000", "kind": "x", "minute": MINUTE}
    assert doc_id(item) == doc_id(dict(item, minute=MINUTE))
    assert doc_id(item) != doc_id(dict(item, minute=MINUTE + 1))


def test_append_parquet_with_no_rows_writes_nothing(out_dir):
    assert append_parquet([], DAY, out_dir) is None
    assert not Path(out_dir).exists()


# —— 告警推送进程 ——

@pytest.fixture(scope="module")
def pusher():
    spec = importlib.util.spec_from_file_location("alert_pusher", "scripts/alert_pusher.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["alert_pusher"] = mod
    spec.loader.exec_module(mod)
    return mod


def decision_item(**over):
    item = {"code": "600000", "name": "浦发银行", "action": "buy", "kind": "limit_up_touch",
            "signal_price": 11.0, "signal_reason": "触及涨停价 11.0", "verdict": "block",
            "blocked_by": "tradability", "risk_reason": "[tradability] 已涨停，买不进",
            "shares": None, "order_amount": None, "fill_price": None, "slippage_bp": None,
            "pushed": 0}
    item.update(over)
    return item


def test_pusher_splits_allowed_and_blocked(client, pusher):
    sent = []
    bus.emit_stream(client, bus.KEY_DECISIONS,
                    [decision_item(), decision_item(kind="vol_ratio_surge", verdict="allow",
                                                    blocked_by="", pushed=1, shares=3800,
                                                    order_amount=39100.0,
                                                    fill_price=10.31, slippage_bp=12.5)])
    p = pusher.AlertPusher(client, url="https://x/robot", flush=0.0,
                           sender=lambda *a: sent.append(a) or True,
                           now=lambda: TS, clock=lambda: 0.0, sleeper=lambda s: None, replay=True)
    assert p.pump() == 2
    p.flush_batch()
    title, text = sent[0][3], sent[0][4]
    assert "可执行" in text and "风控已拦截" in text
    assert "触及涨停" in text and "已涨停，买不进" in text          # 被拦的要说明为什么没推
    assert "建议 3800 股" in text and "3.9 万元" in text
    assert "预估成交 10.31" in text and "12.5bp" in text
    assert title.startswith(f"[{DAY}]")


def test_pusher_starts_at_stream_tail_by_default(client, pusher):
    """默认从流尾开始：重启的告警进程不能把上一批（甚至上一天的）决策当新信号重推。"""
    sent = []
    bus.emit_stream(client, bus.KEY_DECISIONS, [decision_item(verdict="allow", pushed=1)])
    p = pusher.AlertPusher(client, url="https://x", flush=0.0,
                           sender=lambda *a: sent.append(a) or True,
                           now=lambda: TS, clock=lambda: 0.0, sleeper=lambda s: None)
    assert p.pump() == 0                                 # 存量一条不发
    bus.emit_stream(client, bus.KEY_DECISIONS,
                    [decision_item(verdict="allow", pushed=1, kind="bid_stack")])
    assert p.pump() == 1                                 # 启动之后的照发


def test_bus_last_id_is_the_start_cursor(client):
    assert bus.last_id(client, bus.KEY_DECISIONS) == "0-0"     # 空流：从头开始也无所谓
    bus.emit_stream(client, bus.KEY_DECISIONS, [decision_item()])
    tail = bus.last_id(client, bus.KEY_DECISIONS)
    assert tail != "0-0"
    assert bus.read_decisions(client, tail) == []              # 游标本身就是「比它新的才算」


def test_pusher_title_when_everything_is_blocked(client, pusher):
    sent = []
    bus.emit_stream(client, bus.KEY_DECISIONS, [decision_item()])
    p = pusher.AlertPusher(client, url="https://x", flush=0.0,
                           sender=lambda *a: sent.append(a) or True, now=lambda: TS,
                           clock=lambda: 0.0, sleeper=lambda s: None, replay=True)
    p.pump()
    p.flush_batch()
    assert "全部被风控拦下" in sent[0][3]
    assert "本批没有可执行信号" in sent[0][4]


# —— 告警 webhook 取自管理页面的「通知渠道」 ——

def _channels_file(tmp_path, payload):
    p = tmp_path / "notify_channels.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def _ding(name="安全值班群", key="ding_ops", enabled=True,
          url="https://oapi.dingtalk.com/robot/send?access_token=t", secret="enc-secret"):
    return {"key": key, "name": name, "enabled": enabled, "webhook_url": url, "secret": secret}


def test_resolve_webhook_takes_page_config_by_key(tmp_path, pusher):
    """页面是 webhook 的唯一真源：按 key 取，密钥交给后端的解密函数，这里不写死、不落明文。"""
    file = _channels_file(tmp_path, {"dingtalk": [_ding(name="测试群", key="ding_test"), _ding()]})
    got = pusher.resolve_webhook("dingtalk", "ding_ops", file=file,
                                 decrypt=lambda enc: "SEC-" + enc)
    assert got == ("安全值班群", "https://oapi.dingtalk.com/robot/send?access_token=t",
                   "SEC-enc-secret")


def test_resolve_webhook_without_key_skips_disabled(tmp_path, pusher):
    file = _channels_file(tmp_path, {"dingtalk": [
        _ding(name="停用的", key="ding_off", enabled=False),
        _ding(name="启用的", key="ding_on")]})
    name, url, secret = pusher.resolve_webhook("dingtalk", "", file=file, decrypt=lambda e: e)
    assert name == "启用的" and url.startswith("https://oapi.") and secret == "enc-secret"


def test_resolve_webhook_refuses_disabled_and_empty(tmp_path, pusher):
    """点名要一条停用的渠道必须报错：静默不发比报错难查得多。"""
    file = _channels_file(tmp_path, {"dingtalk": [_ding(enabled=False)]})
    with pytest.raises(ValueError, match="停用"):
        pusher.resolve_webhook("dingtalk", "ding_ops", file=file, decrypt=lambda e: e)
    with pytest.raises(ValueError, match="找不到"):
        pusher.resolve_webhook("dingtalk", "nope", file=file, decrypt=lambda e: e)
    empty = _channels_file(tmp_path, {"dingtalk": [_ding(url="")]})
    with pytest.raises(ValueError, match="没有 webhook"):
        pusher.resolve_webhook("dingtalk", "ding_ops", file=empty, decrypt=lambda e: e)


def test_pusher_can_drop_blocked_detail(client, pusher):
    sent = []
    bus.emit_stream(client, bus.KEY_DECISIONS, [decision_item()])
    p = pusher.AlertPusher(client, url="https://x", flush=0.0, include_blocked=False,
                           sender=lambda *a: sent.append(a) or True, now=lambda: TS,
                           clock=lambda: 0.0, sleeper=lambda s: None, replay=True)
    p.pump()
    assert p.blocked == [] and p.batch == []
    assert p.flush_batch() is False                       # 没内容就不发消息


def test_render_caps_each_section_but_titles_the_full_count(pusher):
    """一批上百条时每段截断列前 `cap` 条：超长消息会被机器人直接拒收，静默丢消息更糟。"""
    items = [decision_item(verdict="allow", pushed=1, kind=f"k{i}") for i in range(60)]
    blocked = [decision_item(kind=f"b{i}") for i in range(30)]
    title, text = pusher.render(items, blocked, DAY, datetime(2026, 10, 12, 14, 0))
    assert title == f"[{DAY}] 盘中信号 60 条"                  # 标题是全量，不掩饰截断
    lines = [line for line in text.splitlines() if line.startswith("- ")]
    assert len(lines) == 40 + 30 + 1                    # 放行 40 + 拦截 30（未超）+ 一行指回复盘文件
    assert "其余 20 条见 data/intraday_decisions/2026-10-12.parquet" in text
    assert "另有" not in text                            # 拦截段没超就不提示


def test_pusher_applies_its_cap(client, pusher):
    sent = []
    bus.emit_stream(client, bus.KEY_DECISIONS,
                    [decision_item(verdict="allow", pushed=1, kind=f"k{i}") for i in range(3)])
    p = pusher.AlertPusher(client, url="https://x", flush=0.0, cap=2,
                           sender=lambda *a: sent.append(a) or True,
                           now=lambda: TS, clock=lambda: 0.0, sleeper=lambda s: None,
                           replay=True)
    p.pump()
    p.flush_batch()
    assert "其余 1 条见" in sent[0][4]


def test_pusher_parses_stream_types(client, pusher):
    bus.emit_stream(client, bus.KEY_DECISIONS, [decision_item(shares=3800, slippage_bp=9.4,
                                                              metrics={"avg_amount": 1.0})])
    fields = dict(client.xrange(bus.KEY_DECISIONS)[-1][1])
    item = pusher.parse_item(fields)
    assert item["shares"] == 3800 and item["slippage_bp"] == 9.4
    assert item["metrics"] == {"avg_amount": 1.0}
    assert item["pushed"] == 0


def test_pusher_dingtalk_sign_and_payload_shape(pusher):
    url = pusher.sign_url("https://oapi.dingtalk.com/robot/send?access_token=t", "SECret", TS)
    assert "timestamp=1" in url and "&sign=" in url and url.count("?") == 1
    text = pusher.build_payload("dingtalk", "标题", "正文")
    assert text["markdown"]["title"] == "标题"
    wecom = pusher.build_payload("wecom", "标题", "正文")
    assert "### 标题" in wecom["markdown"]["content"]


def test_pusher_send_survives_bad_json_and_reports_failure(pusher, monkeypatch):
    class Boom:
        def __enter__(self):
            raise OSError("网络不通")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(pusher.urllib.request, "urlopen", lambda *a, **k: Boom())
    assert pusher.send("dingtalk", "https://x", "", "t", "b", sleeper=lambda s: None) is False
