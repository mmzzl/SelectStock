"""链路看门狗：盯的不是信号，是「链路自己还活着吗」。

盘中链路有三个常驻进程（轮询 → 引擎 → 告警），它们坏了的表现是**群里变安静**，而「安静」
和「今天没机会」看起来一模一样，读消息的人分不出来。所以这个进程只做一件事：按 `--status`
同一套心跳口径读 `rt:heartbeat` / `rt:engine-heartbeat`，超阈值就用告警进程同一个渠道说一句
「哪一路停了、为什么」。

三条判据（全部来自那两个进程自己写的心跳，不额外问任何服务）：
- **读不到心跳键**：进程没跑，或 4 小时 TTL 已被清掉——盘中时段里几乎都是前者。
- **心跳龄超阈值**：阈值要给「它自己声明的休眠」让路。轮询拉不到快照会退避到 60 秒、
  休市会睡到 900 秒，这时心跳按声明的节奏在写，不算卡死；拿固定秒数硬卡会在每个节假日误报。
- **上一轮 `status=fetch_failed`**：整轮快照为空（限流/断网），价是旧的，此时算出来的指标
  不可信，这比「没有心跳」更需要立刻知道。

**重复抑制写在 redis**（`rt:watchdog`）而不是进程内存里：看门狗重启不能把上午报过的故障
再报一遍——和盘中告警的去重表同一个理由。**恢复也补一条**并带上中断时长：只报坏不报好，
会让人以为链路一直是坏的。

**只在交易时段判断**：收盘后心跳必然被 TTL 清掉，那是正常收摊不是故障（临时验证用
`--ignore-session`）。读不到 redis 时整轮跳过、一个字都不发：去重表这时候也写不进去，
发消息等于每轮重发一条。

渠道与告警进程同源（后端管理页面「通知渠道」配的机器人，加签密钥发送前才解出明文，
日志只打主机名），env 变量名也复用同一套，方便临时一起切到测试群：
    ALERT_CHANNEL / ALERT_CHANNEL_KEY / ALERT_WEBHOOK_URL / ALERT_WEBHOOK_SECRET

用法:
    python scripts/link_watchdog.py --status                 # 只看现在的链路健康，不发消息
    python scripts/link_watchdog.py --dry-run --rounds 1     # 看它这一刻会发什么
    python scripts/link_watchdog.py --channel-key ding_ops   # 常驻，和告警同一个群
"""

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import alert_pusher
from stock_selector.realtime import bus, session

logger = logging.getLogger("link_watchdog")

STATE_TTL = 18 * 3600        # 与去重表同口径：跨得过交易日，又不让上周的状态留着
QUOTE_MAX_AGE = 150.0        # 轮询正常一拍 5 秒，退避满打满算 60 秒，给两倍余量
ENGINE_MAX_AGE = 180.0        # 引擎一拍里最多跑 max_eval 只 talib，比轮询慢得多
GRACE = 300.0                 # 开盘后前 5 分钟不判故障：心跳键带 4 小时 TTL，那时当天的心跳可能还没写上

BRIEF_KEYS = ("round", "status", "requested", "quotes", "ticks", "changed", "skipped",
              "pool", "evaluated", "decisions", "blocked", "elapsed_s")


@dataclass
class Target:
    """一路被盯的心跳。`label` 是消息里给人看的名字，`key` 是心跳键。"""

    label: str
    key: str
    max_age: float


def inspect_heartbeat(stats: dict, *, max_age: float, now_ts: float) -> str:
    """一份心跳 → 故障说明；返回空字符串表示这一路正常。

    判据顺序是有意的：`fetch_failed` 的心跳是新鲜写上的，按龄永远看不出来，所以先读它自己
    报的状态；缺 `at` 的老心跳按「判不了」处理成故障，而不是当正常——猜错一次的代价是
    整盘没人盯着。
    """
    if not stats:
        return "读不到心跳（进程没跑，或 4 小时 TTL 已过期）"
    status = str(stats.get("status") or "")
    if status == "fetch_failed":
        return (f"上一轮（第 {stats.get('round')} 轮）快照整轮为空，已退避到 "
                f"{float(stats.get('sleep_s') or 0):.0f} 秒——行情是旧的，盘中指标不可信")
    at = float(stats.get("at") or 0)
    if not at:
        return "心跳里没有 at，判不了新旧"
    age = now_ts - at
    limit = max_age + float(stats.get("sleep_s") or 0)
    if age > limit:
        return f"心跳已 {age:.0f} 秒没更新（阈值 {limit:.0f} 秒，上一轮状态 {status or '?'}）"
    return ""


def _brief(stats: dict, now_ts: float) -> str:
    """把心跳里的关键计数压成一行，跟着消息发出去：读的人要能看出「停在第几轮、拉了几只」。"""
    if not stats:
        return "（无）"
    parts = []
    at = float(stats.get("at") or 0)
    if at:
        parts.append(f"龄 {now_ts - at:.0f} 秒")
    parts += [f"{key}={stats[key]}" for key in BRIEF_KEYS if stats.get(key) is not None]
    return "，".join(parts)


def render(target: Target, detail: str, stats: dict, moment: datetime,
           now_ts: float) -> tuple[str, str]:
    title = f"[链路告警] {target.label}异常"
    lines = [f"**{moment:%H:%M:%S}** 检查：{detail}",
             f"- 最后一份心跳：{_brief(stats, now_ts)}",
             "- 下一步：`supervisorctl status` 看这个进程，再看它的日志；恢复后会自动补一条"]
    return title, "\n".join(lines)


def render_recovery(target: Target, since: float, moment: datetime,
                    now_ts: float) -> tuple[str, str]:
    minutes = max(0.0, now_ts - since) / 60.0
    title = f"[链路恢复] {target.label}已恢复"
    text = (f"**{moment:%H:%M:%S}** {target.label}的心跳重新写入了，"
            f"中断约 {minutes:.0f} 分钟。中断期间的决策仍落在当日文件里，可用 "
            f"alert_pusher.py --replay 补发。")
    return title, text


class Watchdog:
    """检查 → 与 redis 里的状态比一比 → 只在状态翻转时发消息。

    先发送、成功后才写状态：发送失败就不记，下一轮重试。反过来（先记再发）会让一条没发出去
    的故障永远静默——那是这条链路里最坏的一种丢消息。
    """

    def __init__(self, client, *, targets, channel=alert_pusher.DINGTALK, url="", secret="",
                 interval=60.0, sender=alert_pusher.send, now=time.time, sleeper=time.sleep,
                 in_window=session.in_trading_window, window_start=session.window_start,
                 grace=GRACE, ignore_session=False, record_state=True):
        self.client = client
        self.targets = list(targets)
        self.channel, self.url, self.secret = channel, url, secret
        self.interval = interval
        self.sender = sender
        self.now = now
        self.sleeper = sleeper
        self.in_window = in_window
        self.window_start = window_start
        self.grace = grace
        self.ignore_session = ignore_session
        # --dry-run 时状态只留在进程里：试跑一次不能把真 redis 的抑制表写脏（那会压掉明早的真告警）
        self.record_state = record_state
        self._memory: dict[str, str] = {}
        self.sent = 0

    def _state(self, label: str) -> dict:
        raw = (self.client.hget(bus.KEY_WATCHDOG, label) if self.record_state
               else self._memory.get(label))
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    def _remember(self, label: str, state=None) -> None:
        """state=None 表示「恢复正常，把这条抑制清掉」。"""
        if self.record_state:
            if state is None:
                self.client.hdel(bus.KEY_WATCHDOG, label)
                return
            self.client.hset(bus.KEY_WATCHDOG, label, json.dumps(state, ensure_ascii=False))
            self.client.expire(bus.KEY_WATCHDOG, STATE_TTL)
            return
        if state is None:
            self._memory.pop(label, None)
        else:
            self._memory[label] = json.dumps(state, ensure_ascii=False)

    def _report(self, target: Target, title: str, text: str, *, state=None) -> bool:
        if not self.sender(self.channel, self.url, self.secret, title, text):
            logger.warning("链路告警发送失败，下一轮重试：%s", title)
            return False
        self.sent += 1
        self._remember(target.label, state)
        logger.info("%s｜%s", title, text.replace("\n", " "))
        return True

    def check(self) -> list[str]:
        """检查全部心跳，返回本轮**新发出**的消息标题（没变化就是空列表）。"""
        now_ts = self.now()
        moment = datetime.fromtimestamp(now_ts)
        if not self.ignore_session and not self.in_window(moment):
            return []
        opened = self.window_start(moment)
        if opened is not None and now_ts - opened.timestamp() < self.grace:
            logger.info("开盘 %.0f 秒宽限内，本轮不判故障", self.grace)
            return []
        titles: list[str] = []
        for target in self.targets:
            stats = bus.last_heartbeat(self.client, key=target.key)
            detail = inspect_heartbeat(stats, max_age=target.max_age, now_ts=now_ts)
            state = self._state(target.label)
            if detail:
                if state:
                    if state.get("detail") != detail:
                        logger.warning("%s 仍未恢复，说明变化：%s → %s",
                                       target.label, state.get("detail"), detail)
                    continue
                title, text = render(target, detail, stats, moment, now_ts)
                if self._report(target, title, text,
                                state={"since": now_ts, "detail": detail}):
                    titles.append(title)
            elif state:
                title, text = render_recovery(target, float(state.get("since") or now_ts),
                                              moment, now_ts)
                if self._report(target, title, text):
                    titles.append(title)
        return titles

    def run(self, rounds: int | None = None) -> None:
        done = 0
        while rounds is None or done < rounds:
            moment = datetime.fromtimestamp(self.now())
            in_session = self.ignore_session or self.in_window(moment)
            if not in_session:
                # 盘外按「距下次开盘还有多久」睡，比固定 60 秒省得多，且开盘前一拍自然醒
                wait = max(self.interval, float(session.sleep_seconds(moment)))
                logger.info("不在交易时段，看门狗休眠 %.0f 秒", wait)
                self.sleeper(wait)
                done += 1
                continue
            try:
                self.check()
            except Exception as exc:  # noqa: BLE001
                # 读不到 redis 就整轮跳过：状态表这时候也写不进去，发消息等于每轮重发一条
                logger.error("链路检查失败，本轮跳过：%s", exc)
            self.sleeper(self.interval)
            done += 1


def build_targets(args) -> list[Target]:
    return [Target("行情轮询", bus.KEY_HEARTBEAT, args.quote_max_age),
            Target("盘中引擎", bus.KEY_ENGINE_HEARTBEAT, args.engine_max_age)]


def show_status(client, targets: list[Target], *, now_ts: float | None = None,
                in_session: bool | None = None) -> int:
    """按看门狗同一套判据打印链路健康，不发消息：`--status` 回答的是「现在有没有超阈值」。"""
    now_ts = now_ts or time.time()
    in_session = session.in_trading_window() if in_session is None else in_session
    print("是否在交易时段:", in_session)
    if not in_session:
        print("（不在交易时段：心跳为空属正常，看门狗此时不会发消息）")
    bad = 0
    for target in targets:
        stats = bus.last_heartbeat(client, key=target.key)
        detail = inspect_heartbeat(stats, max_age=target.max_age, now_ts=now_ts)
        bad += bool(detail)
        print(f"{target.key}（{target.label}）: "
              f"{'正常 — ' + _brief(stats, now_ts) if not detail else detail}")
    print(f"{bus.KEY_TICKS} 长度={bus.stream_len(client, bus.KEY_TICKS)}  "
          f"{bus.KEY_DECISIONS} 长度={bus.stream_len(client, bus.KEY_DECISIONS)}  "
          f"{bus.KEY_QUOTE} 只数={client.hlen(bus.KEY_QUOTE)}")
    print(f"已报故障（抑制中）：{list((client.hgetall(bus.KEY_WATCHDOG) or {}).keys()) or '无'}")
    return 1 if (bad and in_session) else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="盘中链路心跳看门狗（只推送告警，不接券商）")
    p.add_argument("--interval", type=float, default=60.0, help="两轮检查之间的秒数")
    p.add_argument("--quote-max-age", type=float, default=QUOTE_MAX_AGE,
                   help="行情轮询心跳超过多少秒算卡死（会再加上它自己声明的休眠）")
    p.add_argument("--engine-max-age", type=float, default=ENGINE_MAX_AGE,
                   help="盘中引擎心跳超过多少秒算卡死")
    p.add_argument("--grace", type=float, default=GRACE,
                   help="开盘后多少秒内不判故障（心跳键带 4 小时 TTL，刚开盘那几分钟可能还没写上）")
    p.add_argument("--channel", choices=[alert_pusher.DINGTALK, alert_pusher.WECOM],
                   default=os.environ.get("ALERT_CHANNEL", alert_pusher.DINGTALK))
    p.add_argument("--url", default=os.environ.get("ALERT_WEBHOOK_URL", ""))
    p.add_argument("--secret", default=os.environ.get("ALERT_WEBHOOK_SECRET", ""))
    p.add_argument("--channel-key", default=os.environ.get("ALERT_CHANNEL_KEY", ""),
                   help="管理页面里配的渠道 key；不填就用该类型第一条启用的渠道")
    p.add_argument("--ignore-session", action="store_true",
                   help="不在交易时段也检查（只用于临时验证：收盘后心跳为空是正常的）")
    p.add_argument("--rounds", type=int, default=0, help="跑多少轮后退出，0=常驻")
    p.add_argument("--status", action="store_true", help="只打印链路健康，不发消息")
    p.add_argument("--dry-run", action="store_true", help="只打印消息，不真发")
    p.add_argument("--host", default=bus.HOST)
    p.add_argument("--port", type=int, default=bus.PORT)
    p.add_argument("--db", type=int, default=bus.DB)
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    targets = build_targets(args)
    client = bus.connect(args.host, args.port, args.db)
    if args.status:
        return show_status(client, targets)

    sender = alert_pusher.print_sender
    if not args.dry_run:
        if not args.url:
            try:
                name, args.url, args.secret = alert_pusher.resolve_webhook(
                    args.channel, args.channel_key)
                logger.info("告警渠道取自管理页面：%s（%s，%s，加签密钥%s）", name, args.channel,
                            alert_pusher.redacted(args.url), "已解出" if args.secret else "为空")
            except Exception as exc:  # noqa: BLE001 配置读不到就是没配好，全部指向同一条出路
                raise SystemExit(f"读不到页面配的{args.channel}渠道：{exc}\n"
                                 "可在页面「通知渠道」里配好，或设 ALERT_WEBHOOK_URL，"
                                 "或先用 --dry-run 看消息") from exc
        sender = alert_pusher.send

    watchdog = Watchdog(client, targets=targets, channel=args.channel, url=args.url,
                        secret=args.secret, interval=args.interval, sender=sender,
                        ignore_session=args.ignore_session, grace=args.grace,
                        record_state=not args.dry_run)
    logger.info("看门狗启动：每 %.0f 秒检查 %d 路心跳（%s），阈值 %s", args.interval,
                len(targets), "、".join(f"{t.label}={t.max_age:.0f}s" for t in targets),
                "盘外也检查" if args.ignore_session else "仅交易时段")
    watchdog.run(rounds=args.rounds or None)
    logger.info("退出：共发出 %d 条链路消息", watchdog.sent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
