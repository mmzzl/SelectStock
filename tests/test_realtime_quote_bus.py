"""实时链路：redis 键约定、交易时段判定与快照轮询的三态（正常/休市/拉不到）。

redis 用内存假客户端：测试不碰真服务，键行为（hash/stream/TTL）由假客户端自己记账，
断言的是「写了什么」而不是「redis 怎么实现」。时钟一律注入，否则「现在是不是盘中」
会让用例随跑测试的时间点翻脸。
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stock_selector.realtime import bus, session                        # noqa: E402
from redis_fake import FakeRedis                                # noqa: E402  (与盘中引擎测试共用)
from stock_selector.realtime.poller import (QuotePoller, changed,       # noqa: E402
                                            resolve_universe, to_tick)

# 交易时段内的固定「现在」：2026-10-12 是周一
NOW = datetime(2026, 10, 12, 9, 30, 0)
TODAY = "20261012"


@pytest.fixture
def client():
    return FakeRedis()


def quote(code="600000", price=10.0, **over):
    q = {"code": code, "name": "浦发银行", "price": price, "pct": 1.2, "volume": 1000.0,
         "amount": 1e7, "turnover_pct": 0.5, "vol_ratio": 1.1, "limit_up": 11.0,
         "limit_down": 9.0, "bids": [[10.0, 100]], "asks": [[10.01, 200]],
         "quote_time": f"{TODAY}093000"}
    q.update(over)
    return q


def make(codes, fetch, client, **over):
    kw = {"now": lambda: NOW, "clock": lambda: 0.0, "sleeper": lambda s: None}
    kw.update(over)
    return QuotePoller(client, codes, fetch=fetch, **kw)


# —— bus：键约定 ——

def test_put_quotes_writes_json_and_ttl(client):
    assert bus.put_quotes(client, {"600000": quote(), "000001": quote("000001")}) == 2
    assert json.loads(client.hget(bus.KEY_QUOTE, "600000"))["price"] == 10.0
    assert client.ttls[bus.KEY_QUOTE] == bus.QUOTE_TTL


def test_put_quotes_empty_writes_nothing(client):
    assert bus.put_quotes(client, {}) == 0
    assert client.hashes == {}


def test_latest_full_read_and_by_code(client):
    bus.put_quotes(client, {"600000": quote()})
    assert set(bus.latest(client)) == {"600000"}
    # 读不到的代码不出现，也不补一个空快照——下游据此区分「没数据」和「价格为 0」
    assert set(bus.latest(client, ["600000", "000001"])) == {"600000"}


def test_latest_skips_corrupt_value(client):
    client.hset(bus.KEY_QUOTE, field="600000", value="not-json")
    client.hset(bus.KEY_QUOTE, field="000001", value=json.dumps(quote("000001")))
    assert list(bus.latest(client)) == ["000001"]


def test_emit_ticks_flattens_and_caps_stream(client):
    ticks = [to_tick("600000", quote(), 1.0), to_tick("000001", quote("000001"), 2.0)]
    assert bus.emit_ticks(client, ticks, maxlen=1) == 2
    entries = client.streams[bus.KEY_TICKS]
    assert len(entries) == 1                               # MAXLEN 裁掉最早的
    assert entries[0][1]["code"] == "000001"
    assert entries[0][1]["price"] == "10.0"                # stream field 只能放字符串


def test_emit_ticks_drops_none_and_keeps_book_as_json(client):
    bus.emit_ticks(client, [{"code": "600000", "price": None, "pct": 1.0, "bids": [[1.0, 2]]}])
    fields = client.streams[bus.KEY_TICKS][0][1]
    assert "price" not in fields
    assert json.loads(fields["bids"]) == [[1.0, 2]]


def test_read_ticks_skips_cursor(client):
    bus.emit_ticks(client, [to_tick("600000", quote(), 1.0)])
    entries = bus.read_ticks(client)
    assert len(entries) == 1
    assert bus.read_ticks(client, entries[0][0]) == []     # 游标自身不重复消费
    bus.emit_ticks(client, [to_tick("000001", quote("000001"), 2.0)])
    follow = bus.read_ticks(client, entries[0][0])
    assert [f["code"] for _, f in follow] == ["000001"]
    assert bus.stream_len(client) == 2


def test_heartbeat_and_metrics(client):
    bus.heartbeat(client, {"round": 3, "status": "ok", "at": 1.0})
    assert bus.last_heartbeat(client)["round"] == 3
    assert client.ttls[bus.KEY_HEARTBEAT] == bus.QUOTE_TTL

    bus.put_metrics(client, "600000", {"ma5": 10.1, "vol_ratio": 1.4, "missing": None})
    assert bus.get_metrics(client, "600000") == {"ma5": "10.1", "vol_ratio": "1.4"}
    assert client.ttls[bus.metric_key("600000")] == bus.METRIC_TTL


# —— session：什么时候该跑 ——

def at(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")


@pytest.mark.parametrize("moment,expected", [
    ("2026-10-12 09:24:59", False),       # 集合竞价尾段之前
    ("2026-10-12 09:25:00", True),
    ("2026-10-12 11:29:59", True),
    ("2026-10-12 11:30:00", False),       # 午间休市
    ("2026-10-12 12:59:59", False),
    ("2026-10-12 13:00:00", True),
    ("2026-10-12 15:04:59", True),
    ("2026-10-12 15:05:00", False),       # 收盘
    ("2026-10-11 09:30:00", False),       # 周日
])
def test_trading_window_margins(moment, expected):
    assert session.in_trading_window(at(moment)) is expected


@pytest.mark.parametrize("moment,expected", [
    ("2026-10-12 09:25:30", "2026-10-12 09:25:00"),
    ("2026-10-12 11:00:00", "2026-10-12 09:25:00"),
    ("2026-10-12 13:00:10", "2026-10-12 13:00:00"),
    ("2026-10-12 11:30:00", None),          # 午间休市：没有「本段开盘时刻」
    ("2026-10-12 15:30:00", None),
    ("2026-10-11 09:30:00", None),          # 周日
])
def test_window_start_is_the_current_segment(moment, expected):
    """看门狗用它算开盘宽限：只有「正在时段里」才有起点，盘外返回 None。"""
    start = session.window_start(at(moment))
    assert (None if start is None else start.strftime("%Y-%m-%d %H:%M:%S")) == expected


def test_sleep_targets_next_window_and_is_capped():
    assert session.seconds_to_next_window(at("2026-10-12 09:00:00")) == 25 * 60
    assert session.seconds_to_next_window(at("2026-10-12 12:00:00")) == 60 * 60
    assert session.seconds_to_next_window(at("2026-10-12 15:30:00")) == 64500   # 明天 09:25
    # 离开盘还远就只睡 MAX_SLEEP，醒来重判：调休/节假日靠这个逐日复判
    assert session.sleep_seconds(at("2026-10-12 15:20:00")) == session.MAX_SLEEP
    assert session.sleep_seconds(at("2026-10-12 09:20:00")) == 300


def test_snapshot_is_today_uses_exchange_timestamp():
    assert session.snapshot_is_today("20261012093000", NOW) is True
    assert session.snapshot_is_today("20200101093000", NOW) is False
    assert session.snapshot_is_today("", NOW) is False         # 没时间戳宁可不发
    assert session.snapshot_is_today("not-a-time", NOW) is False
    assert session.snapshot_is_today("20261012150003", at("2026-10-12 15:00:10")) is True


# —— poller：三态 ——

def test_round_writes_only_changed_ticks(client):
    seen = [{"600000": quote(price=10.0)}, {"600000": quote(price=10.0)},
            {"600000": quote(price=10.1)}]
    poller = make(["600000"], lambda codes: seen.pop(0), client)
    assert poller.round()["ticks"] == 1                    # 首拍必发，否则下游没有初始状态
    assert poller.round()["ticks"] == 0                    # 一模一样不发
    assert poller.round()["ticks"] == 1                    # 价格动了才发
    assert json.loads(client.hget(bus.KEY_QUOTE, "600000"))["price"] == 10.1


def test_round_reports_fetch_failure_and_backs_off(client):
    poller = make(["600000"], lambda codes: {}, client, interval=5.0)
    stats = poller.round()
    assert stats["status"] == "fetch_failed" and stats["sleep_s"] == 10.0
    assert poller.round()["sleep_s"] == 20.0               # 指数退避，别去撞限流
    assert client.streams == {}                             # 空轮不写流
    assert bus.last_heartbeat(client)["status"] == "fetch_failed"


def test_backoff_is_capped_and_resets(client):
    poller = make(["600000"], lambda codes: {}, client, interval=5.0)
    for _ in range(6):
        stats = poller.round()
    assert stats["sleep_s"] == 60.0                        # 再大就错过行情
    poller.fetch = lambda codes: {"600000": quote()}
    assert poller.round()["sleep_s"] == poller.interval    # 恢复即回到正常节奏


def test_round_skips_stale_snapshot_as_market_closed(client):
    stats = make(["600000"], lambda codes: {"600000": quote(quote_time="20200101093000")},
                 client).round()
    assert stats["status"] == "market_closed"
    assert client.hashes == {} and client.streams == {}    # 昨天的价不能当实时价推下去


def test_round_mixes_fresh_and_stale(client):
    quotes = {"600000": quote(), "000001": quote("000001", quote_time="20200101093000")}
    stats = make(["600000", "000001"], lambda codes: quotes, client).round()
    assert stats["stale"] == 1 and stats["quotes"] == 1
    assert list(bus.latest(client)) == ["600000"]


def test_to_tick_carries_decision_fields():
    tick = to_tick("600000", quote(), 123.456)
    assert set(tick) == {"code", "ts", *poller_fields()}
    assert tick["ts"] == 123.456 and tick["limit_up"] == 11.0 and tick["amount"] == 1e7


def poller_fields():
    from stock_selector.realtime.poller import TICK_FIELDS

    return TICK_FIELDS


def test_changed_ignores_book_only_moves():
    prev = quote(price=10.0, bids=[[10.0, 100]])
    assert changed(prev, quote(price=10.0, bids=[[10.0, 800]])) is False
    assert changed(prev, quote(price=10.01)) is True
    assert changed(None, quote()) is True


def test_resolve_universe_dedupes_and_limits():
    assert resolve_universe(["600000", "000001", "600000"]) == ["600000", "000001"]
    assert resolve_universe(["600000", "000001"], limit=1) == ["600000"]
    assert resolve_universe([]) == []


def test_resolve_universe_all_market_uses_parquet_pool(monkeypatch):
    from stock_selector.datasource.parquet_source import ParquetSource

    monkeypatch.setattr(ParquetSource, "get_stock_list",
                        lambda self: [{"code": "1"}, {"code": "600000"}])
    assert resolve_universe(["000002"], all_market=True) == ["000002", "000001", "600000"]


def test_run_sleeps_when_outside_window(client):
    slept = []
    poller = make(["600000"], lambda codes: {"600000": quote()}, client,
                  now=lambda: datetime(2026, 10, 11, 20, 0, 0), sleeper=slept.append)
    poller.run(rounds=2)
    assert poller.rounds == 0 and len(slept) == 2          # 周末：一轮行情都不拉
    assert slept[0] == session.MAX_SLEEP


def test_run_polls_and_sleeps_interval(client):
    slept = []
    poller = make(["600000"], lambda codes: {"600000": quote()}, client,
                  interval=7.0, sleeper=slept.append)
    poller.run(rounds=2)
    assert poller.rounds == 2 and slept == [7.0, 7.0]


# —— 换日重取股票池 ——

def test_refresh_swaps_codes_and_drops_yesterday_prices(client):
    """刷新后要清掉上一拍缓存：隔夜价不可比，否则「有没有变化」在拿昨天当基准。"""
    poller = make(["600000"], lambda codes: {}, client, universe_loader=lambda: ["1", "600000"])
    poller.prev["600000"] = quote(price=99.0)
    assert poller.refresh() == 2
    assert poller.codes == ["000001", "600000"]        # 补齐六位：app.db 存的是短码
    assert poller.prev == {}


def test_refresh_without_loader_keeps_given_codes(client):
    poller = make(["600000", "000001"], lambda codes: {}, client)
    assert poller.refresh() == 2 and poller.codes == ["600000", "000001"]


def test_refresh_keeps_previous_pool_when_loader_fails(client, caplog):
    """读库失败不能让行情链路崩掉，也不能悄悄变成「今天一只都不看」。"""
    def boom():
        raise RuntimeError("app.db 读不到")

    poller = make(["600000"], lambda codes: {}, client, universe_loader=boom)
    with caplog.at_level("WARNING"):
        assert poller.refresh() == 1
    assert poller.codes == ["600000"] and "沿用上一批" in caplog.text


def test_refresh_keeps_previous_pool_when_empty(client, caplog):
    poller = make(["600000"], lambda codes: {}, client, universe_loader=lambda: [])
    with caplog.at_level("WARNING"):
        assert poller.refresh() == 1
    assert poller.codes == ["600000"] and "刷新为空" in caplog.text


def test_run_refreshes_pool_on_day_change(client):
    """睡一觉跨到新交易日必须重取池：第二天盯的是前一天盘后选出来的名单。"""
    moment = {"now": NOW}
    asked, batches = [], [["000001", "600519"]]

    def fetch(codes):
        asked.append(list(codes))
        return {c: quote(c) for c in codes}

    def sleeper(_seconds):
        moment["now"] = datetime(2026, 10, 13, 9, 30, 0)

    poller = make(["600000"], fetch, client, now=lambda: moment["now"], sleeper=sleeper,
                  universe_loader=lambda: batches.pop(0))
    poller.run(rounds=2)
    assert asked == [["600000"], ["000001", "600519"]]
    assert batches == []                                # 启动那一轮不该多读一次库


def test_run_keeps_pool_when_same_day(client):
    moment = {"now": NOW}
    asked, reads = [], [0]

    def fetch(codes):
        asked.append(list(codes))
        return {c: quote(c) for c in codes}

    def loader():
        reads[0] += 1
        return ["000001"]

    poller = make(["600000"], fetch, client, now=lambda: moment["now"],
                  sleeper=lambda _s: None, universe_loader=loader)
    poller.run(rounds=3)
    assert asked == [["600000"]] * 3 and reads == [0]
