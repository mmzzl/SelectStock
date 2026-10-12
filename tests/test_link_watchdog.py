"""链路看门狗：心跳判据、只在状态翻转时发消息、抑制与恢复状态写在 redis。

时钟、交易时段、发送全部注入：判据本质是「现在几点、心跳几点写的」，用真时钟的用例会在
收盘后集体变成误报。redis 用 tests/redis_fake.py 的内存假客户端。
"""

import importlib.util
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from redis_fake import FakeRedis                                        # noqa: E402
from stock_selector.realtime import bus                                 # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "link_watchdog", ROOT / "scripts" / "link_watchdog.py")
watchdog_mod = importlib.util.module_from_spec(_SPEC)
sys.modules["link_watchdog"] = watchdog_mod
_SPEC.loader.exec_module(watchdog_mod)

Target = watchdog_mod.Target
Watchdog = watchdog_mod.Watchdog
inspect_heartbeat = watchdog_mod.inspect_heartbeat
show_status = watchdog_mod.show_status

# 2026-10-12 是周一，10:00 在早盘时段内
NOW = datetime(2026, 10, 12, 10, 0, 0)
TS = NOW.timestamp()


@pytest.fixture
def client():
    return FakeRedis()


def targets(quote_age=150.0, engine_age=180.0):
    return [Target("行情轮询", bus.KEY_HEARTBEAT, quote_age),
            Target("盘中引擎", bus.KEY_ENGINE_HEARTBEAT, engine_age)]


def beat(client, *, at=TS, status="ok", key=bus.KEY_HEARTBEAT, **over):
    """写一份心跳（默认「刚刚写好、状态正常」）。"""
    stats = {"at": at, "status": status, "round": 7, "requested": 360, "quotes": 360}
    stats.update(over)
    bus.heartbeat(client, stats, key=key)


def build(client, *, in_window=True, fail_send=False, clock=None, sleepers=None, **over):
    sent = []

    def sender(channel, url, secret, title, text):
        if fail_send:
            return False
        sent.append((channel, url, title, text))
        return True

    watch = Watchdog(client, targets=targets(), channel="dingtalk", url="https://x/robot",
                     sender=sender, now=lambda: (clock or {}).get("t", TS),
                     sleeper=lambda seconds: None if sleepers is None else sleepers.append(seconds),
                     in_window=lambda moment: in_window, **over)
    return watch, sent


# —— 判据 ——

def test_missing_heartbeat_is_a_fault(client):
    detail = inspect_heartbeat({}, max_age=150.0, now_ts=TS)
    assert "读不到心跳" in detail


def test_stale_heartbeat_reports_age_and_threshold(client):
    detail = inspect_heartbeat({"at": TS - 300, "status": "ok"}, max_age=150.0, now_ts=TS)
    assert "300 秒没更新" in detail and "阈值 150 秒" in detail


def test_declared_sleep_extends_the_threshold(client):
    """节假日轮询按声明睡 900 秒：这段时间心跳不写不算卡死，否则每个休市日上午都要误报一次。"""
    stats = {"at": TS - 400, "status": "market_closed", "sleep_s": 900}
    assert inspect_heartbeat(stats, max_age=150.0, now_ts=TS) == ""


def test_fetch_failed_alerts_even_with_fresh_heartbeat(client):
    """数据源整轮为空时心跳是新鲜的，按龄永远看不出来——但价是旧的，指标不可信。"""
    stats = {"at": TS - 5, "status": "fetch_failed", "sleep_s": 60, "round": 88}
    detail = inspect_heartbeat(stats, max_age=150.0, now_ts=TS)
    assert "快照整轮为空" in detail and "第 88 轮" in detail


def test_heartbeat_without_at_is_not_silently_ok(client):
    assert "at" in inspect_heartbeat({"status": "ok"}, max_age=150.0, now_ts=TS)


# —— 消息只发在状态翻转的那一刻 ——

def test_both_dead_in_window_sends_two_messages(client):
    watch, sent = build(client)
    assert watch.check() == [t for _, _, t, _ in sent]      # 标题与实发一致
    assert [t for _, _, t, _ in sent] == ["[链路告警] 行情轮询异常", "[链路告警] 盘中引擎异常"]
    assert watch.check() == []                       # 第二次不再重发
    assert "supervisorctl" in sent[0][3]
    assert list(client.hgetall(bus.KEY_WATCHDOG)) == ["行情轮询", "盘中引擎"]


def test_outside_trading_window_stays_silent(client):
    """收盘后心跳必然被 TTL 清掉，那是正常收摊：此时一条都不能发。"""
    watch, sent = build(client, in_window=False)
    assert watch.check() == []
    assert sent == []
    assert client.hgetall(bus.KEY_WATCHDOG) == {}


def test_grace_after_open_sends_nothing(client):
    """刚开盘那几分钟进程可能还没写下当天第一份心跳（盘外最长睡 900 秒），此时判故障是天天误报。"""
    opened = datetime(2026, 10, 12, 9, 25, 0)
    clock = {"t": opened.timestamp() + 60}
    watch, sent = build(client, clock=clock, window_start=lambda moment: opened)
    assert watch.check() == [] and sent == []
    clock["t"] = opened.timestamp() + 600                 # 宽限过后仍然空白 → 该报了
    assert len(watch.check()) == 2


def test_healthy_heartbeats_send_nothing(client):
    beat(client)
    beat(client, key=bus.KEY_ENGINE_HEARTBEAT)
    watch, sent = build(client)
    assert watch.check() == [] and sent == []


def test_recovery_is_announced_with_duration(client):
    clock = {"t": TS}
    watch, sent = build(client, clock=clock)
    assert len(watch.check()) == 2
    clock["t"] = TS + 600                            # 十分钟后两路心跳都重新写上了
    beat(client, at=TS + 600)
    beat(client, at=TS + 600, key=bus.KEY_ENGINE_HEARTBEAT)
    assert watch.check() == ["[链路恢复] 行情轮询已恢复", "[链路恢复] 盘中引擎已恢复"]
    assert "中断约 10 分钟" in sent[-2][3]
    assert client.hgetall(bus.KEY_WATCHDOG) == {}     # 状态清掉了，下次再坏会重新报


def test_partial_recovery_only_announces_that_leg(client):
    watch, sent = build(client)
    watch.check()
    beat(client)                                     # 轮询活了，引擎仍然没心跳
    assert watch.check() == ["[链路恢复] 行情轮询已恢复"]
    assert list(client.hgetall(bus.KEY_WATCHDOG)) == ["盘中引擎"]


def test_send_failure_does_not_record_state(client):
    """发失败就不记状态：记了等于这条故障永久静默，那是最坏的一种丢消息。"""
    watch, sent = build(client, fail_send=True)
    assert watch.check() == [] and sent == []
    assert client.hgetall(bus.KEY_WATCHDOG) == {}


def test_state_in_redis_survives_restart(client):
    """重启的看门狗不能把上午报过的故障再报一遍——状态在 redis，不在进程内存。"""
    first, sent = build(client)
    assert len(first.check()) == 2 and len(sent) == 2
    _, resent = build(client)
    assert resent == []


def test_dry_run_does_not_dirty_the_state_table(client):
    """试跑只打印：写进真 redis 的话，明早的真故障会被这次试跑的状态压掉。"""
    watch, sent = build(client, record_state=False)
    assert len(watch.check()) == 2
    assert client.hgetall(bus.KEY_WATCHDOG) == {}
    assert len(watch.check()) == 0                    # 进程内仍然抑制，不刷屏


def test_changed_detail_while_still_broken_is_logged_not_resent(client):
    clock = {"t": TS}
    watch, sent = build(client, clock=clock)
    assert len(watch.check()) == 2                   # 两路都没心跳，各报一条
    clock["t"] = TS + 600                            # 十分钟后仍未恢复，说明里的龄变了
    assert watch.check() == []
    assert len(sent) == 2


# —— 常驻循环 ——

def test_run_sleeps_long_outside_window_and_skips_check(client):
    waits = []
    watch, sent = build(client, in_window=False, sleepers=waits)
    watch.run(rounds=1)
    assert waits == [900.0]                          # 睡到 MAX_SLEEP，而不是按 60 秒空转
    assert sent == []


def test_redis_error_skips_round_without_sending(client):
    """读不到 redis 时状态表也写不进去，此时每轮发一条就是刷屏——只留日志。"""

    class Broken(FakeRedis):
        def hget(self, key, field):
            raise ConnectionError("redis 挂了")

    watch, sent = build(Broken())
    watch.run(rounds=1)
    assert sent == []


# —— --status ——

def test_status_prints_verdict_and_returns_nonzero_in_window(client, capsys):
    beat(client, at=TS - 900)
    beat(client, key=bus.KEY_ENGINE_HEARTBEAT)
    assert show_status(client, targets(), now_ts=TS, in_session=True) == 1
    out = capsys.readouterr().out
    assert "行情轮询" in out and "900 秒没更新" in out
    assert "盘中引擎" in out and "正常" in out


def test_status_after_close_is_not_a_failure(client, capsys):
    """盘外 --status 报 rc=0：心跳为空是正常的，否则运维脚本每天收盘后都在报错。"""
    assert show_status(client, targets(), now_ts=TS, in_session=False) == 0
    assert "不在交易时段" in capsys.readouterr().out
