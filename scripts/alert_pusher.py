"""告警推送：消费 redis `rt:decisions`，把过完风控的最终决策发到钉钉/企业微信机器人。

链路里「告警/交易模块」那一格，按需求**只推送消息，不接券商下单**。

两条设计约束：
- **一批一条消息**：盘中决策是持续流，一条发一条会把群刷爆、也很容易撞到机器人的 20 条/分钟
  限流。攒 `--flush` 秒合成一条 markdown，读的人一眼看完这一波。
- **被风控拦下的也要说**：拦下的信号照样进决策流（`verdict=block`），摘要里单列一段写清
  「哪只票的什么信号、被哪条规则拦了、为什么」。不然是「静默不推」，用户无法区分
  「没有机会」和「有机会但下不了单」——这两种情况的后续动作完全不同。
- **只发启动之后的决策**：进程启动时把游标放在流尾，重启不会把上一交易日的存量当新信号重推
  （要重发显式 `--replay`）。

Webhook 默认取自后端管理页面的「通知渠道」配置（`notify_channels.json`，密钥在那边是
AES-GCM 密文，本进程发送前解出明文，绝不写进文件也不打日志）。环境变量只用来覆盖它，
比如临时发到测试群（钉钉 webhook 的 access_token 和 sign 都在 URL 里）：
    ALERT_CHANNEL        dingtalk | wecom（默认 dingtalk）
    ALERT_CHANNEL_KEY    页面里那条渠道的 key；不填取该类型第一条启用的
    ALERT_WEBHOOK_URL    直接给地址，跳过页面配置
    ALERT_WEBHOOK_SECRET 钉钉机器人的加签密钥（企业微信不需要）

用法:
    python scripts/alert_pusher.py                            # 用页面配的钉钉渠道
    export ALERT_WEBHOOK_URL='https://oapi.dingtalk.com/robot/send?access_token=xxx'
    export ALERT_WEBHOOK_SECRET='SECxxxx'                     # 或显式覆盖
    python scripts/alert_pusher.py --dry-run              # 只打印，不真发
    python scripts/alert_pusher.py                        # 常驻
    python scripts/alert_pusher.py --rounds 1 --dry-run    # 冒烟
"""

import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stock_selector.realtime import bus

logger = logging.getLogger("alert_pusher")

BACKEND_DIR = Path(__file__).resolve().parent.parent / "stock_selector_api" / "backend"

KIND_LABEL = {
    "ma_golden_cross": "均线金叉", "ma_dead_cross": "均线死叉",
    "macd_golden_cross": "MACD 金叉", "macd_dead_cross": "MACD 死叉",
    "rsi_oversold_bounce": "RSI 超卖回升", "rsi_overbought": "RSI 超买",
    "boll_break_up": "突破布林上轨", "boll_break_dn": "跌破布林下轨",
    "volume_blast_up": "放量拉升", "volume_surge_drop": "放量大跌",
    "vol_ratio_surge": "量比放大", "turnover_hot": "换手过高",
    "breakout_resistance": "上穿 20 日压力位", "support_break": "跌破 20 日支撑位",
    "order_flow_inflow": "打单流入", "order_flow_outflow": "打单流出",
    "bid_stack": "买盘托单", "ask_stack": "卖盘压单",
    "limit_up_touch": "触及涨停", "limit_down_touch": "触及跌停",
    "event_bullish": "利多事件", "event_bearish": "利空事件",
}
ACTION_LABEL = {"buy": "买入", "sell": "卖出"}
DINGTALK, WECOM = "dingtalk", "wecom"


def sign_url(url: str, secret: str, moment: float) -> str:
    """钉钉加签：timestamp + HMAC-SHA256(secret, "{timestamp}\\n{secret}")，base64 后 urlencode。"""
    timestamp = str(int(moment * 1000))
    digest = hmac.new(secret.encode(), f"{timestamp}\n{secret}".encode(),
                      hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest))
    join = "&" if "?" in url else "?"
    return f"{url}{join}timestamp={timestamp}&sign={sign}"


def build_payload(channel: str, title: str, text: str) -> dict:
    if channel == WECOM:
        return {"msgtype": "markdown", "markdown": {"content": f"### {title}\n{text}"}}
    return {"msgtype": "markdown", "markdown": {"title": title, "text": text},
            "at": {"isAtAll": False}}


def redacted(url: str) -> str:
    """日志里只留主机名：access_token/sign 都在 query 里，打出来就等于泄露机器人。"""
    return urllib.parse.urlsplit(url).netloc


def resolve_webhook(channel: str, key: str = "", *, file=None, decrypt=None) -> tuple[str, str, str]:
    """从后端「通知渠道」配置里取机器人地址：返回 `(名称, url, 加签密钥)`。

    这份 JSON 是管理页面写的唯一真源，密钥以 AES-GCM 密文落盘，所以解密要用后端的
    `core.security`（密钥派生自 `backend/.env` 的 jwt_secret）——在这里解，是为了不再
    把同一个机器人抄第二份到 supervisor 环境变量里：页面改了 webhook，进程重启即生效。
    解出来的明文只活在这一个元组里，既不写文件也不打日志。
    """
    if file is None or decrypt is None:
        sys.path.insert(0, str(BACKEND_DIR))
        from config import get_settings
        from core.security import decrypt_secret
        file = file or get_settings().notify_channels_file
        decrypt = decrypt or decrypt_secret

    entries = json.loads(Path(file).read_text(encoding="utf-8") or "{}").get(channel) or []
    chosen = None
    for item in entries:
        if key:
            if item.get("key") == key:
                chosen = item
                break
        elif item.get("enabled", True):
            chosen = item
            break
    if chosen is None:
        raise ValueError(f"渠道配置里找不到{' ' + channel + ' 的 ' + key if key else '可用的 ' + channel}"
                         f"（{file}）")
    if not chosen.get("enabled", True):
        raise ValueError(f"渠道 {chosen.get('key')} 在页面上是停用状态")
    url = (chosen.get("webhook_url") or "").strip()
    if not url:
        raise ValueError(f"渠道 {chosen.get('key')} 没有 webhook 地址")
    return str(chosen.get("name") or chosen.get("key")), url, decrypt(chosen.get("secret") or "")


def send(channel: str, url: str, secret: str, title: str, text: str,
         timeout: float = 6.0, sleeper=time.sleep, retries: int = 2) -> bool:
    """POST 到机器人 webhook。失败重试 `retries` 次后放弃并留日志：决策已落盘，消息可丢。"""
    body = json.dumps(build_payload(channel, title, text)).encode("utf-8")
    target = sign_url(url, secret, time.time()) if (channel == DINGTALK and secret) else url
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(target, data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read().decode("utf-8", "replace") or "{}")
            if int(result.get("errcode", 0) or 0) == 0:
                return True
            logger.warning("机器人返回错误 %s（渠道 %s）: %s",
                           result.get("errcode"), redacted(url), result.get("errmsg"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            logger.warning("告警发送失败 %d/%d（渠道 %s）: %s", attempt + 1, retries + 1,
                           redacted(url), exc)
        if attempt < retries:
            sleeper(2.0 * (attempt + 1))
    return False


def fmt_line(item: dict, *, blocked: bool = False) -> str:
    """一条决策一行。数值全部来自引擎与风控，这里只做单位与排版（不做任何算术）。

    拦截行要说的是**风控为什么拦**（`risk_reason`，里面带着规则名），而不是信号自己写了
    什么——读的人已经知道触板了，他不知道的是这一单下不去。
    """
    label = KIND_LABEL.get(item.get("kind"), item.get("kind") or "-")
    head = (f"- {item.get('name') or ''} {item.get('code')}｜"
            f"{ACTION_LABEL.get(item.get('action'), item.get('action'))}｜{label}"
            f"｜{item.get('signal_price')}")
    if blocked:
        detail = f"风控：{item.get('risk_reason') or item.get('signal_reason') or '未说明'}"
        return "\n".join(part for part in (head, detail) if part)
    detail = item.get("signal_reason") or ""
    shares, amount = item.get("shares"), item.get("order_amount")
    plan = []
    if shares:
        plan.append(f"建议 {shares} 股")
    if amount:
        plan.append(f"{amount / 1e4:.1f} 万元")
    if item.get("fill_price"):
        plan.append(f"预估成交 {item.get('fill_price')}")
    if item.get("slippage_bp"):
        plan.append(f"预估滑点 {item.get('slippage_bp')}bp")
    tail = ("；".join(plan)) if plan else ""
    return "\n".join(part for part in (head, detail, tail and f"  {tail}") if part)


def render(items: list[dict], blocked: list[dict], day: str, moment: datetime,
           cap: int = 40) -> tuple[str, str]:
    """一批决策 → (标题, markdown 正文)。放行与拦截分列，拦截必须带原因。

    `cap` 是每段最多列几条：全市场盘中一批可能上百条，而机器人对消息长度有上限——宁可截断
    并指回当日决策文件，也不要发出一条被平台拒收的长消息（标题里的条数仍是全量，不掩饰）。
    """
    title = f"[{day}] 盘中信号 {len(items)} 条"
    lines = [f"**{moment:%H:%M:%S}** 更新"]
    if items:
        lines += ["", "### 可执行", *[fmt_line(item) for item in items[:cap]]]
        if len(items) > cap:
            lines.append(f"- 其余 {len(items) - cap} 条见 data/intraday_decisions/{day}.parquet")
    else:
        title = f"[{day}] 本批 {len(blocked)} 条信号全部被风控拦下"
        lines += ["", "本批没有可执行信号。"]
    if blocked:
        lines += ["", "### 风控已拦截（不给下单建议，原因如下）",
                  *[fmt_line(item, blocked=True) for item in blocked[:cap]]]
        if len(blocked) > cap:
            lines.append(f"- 另有 {len(blocked) - cap} 条同样被拦下，明细在当日决策文件")
    return title, "\n".join(lines)


def parse_item(fields: dict) -> dict:
    """stream 里全是字符串：数值列转回来，`metrics` 还原成字典。"""
    out = {}
    for key, value in fields.items():
        if key == "metrics":
            try:
                out[key] = json.loads(value)
            except ValueError:
                out[key] = {}
            continue
        try:
            out[key] = int(value)
            continue
        except ValueError:
            pass
        try:
            out[key] = float(value)
            continue
        except ValueError:
            out[key] = value
    return out


class AlertPusher:
    """攒批 + 发送 + 游标推进。发送失败不重试整批以外的逻辑：批已经发过就算发过。

    默认从**流尾**开始（`bus.last_id`）：进程重启不能把上一交易日的决策当新信号重推。
    要把存量重发一次（补消息、或调试排版）显式给 `replay=True`。
    """

    def __init__(self, client, *, channel=DINGTALK, url="", secret="", flush=30.0,
                 include_blocked=True, sender=send, now=time.time, clock=time.monotonic,
                 sleeper=time.sleep, replay=False, cap=40):
        self.client = client
        self.channel, self.url, self.secret = channel, url, secret
        self.flush = flush
        self.include_blocked = include_blocked
        self.cap = cap
        self.sender = sender
        self.now = now
        self.clock = clock
        self.sleeper = sleeper
        self.cursor = "0-0" if replay else bus.last_id(client, bus.KEY_DECISIONS)
        self.batch: list[dict] = []
        self.blocked: list[dict] = []
        self.started = self.clock()
        self.sent = 0
        self.failed = 0

    def pump(self) -> int:
        """读一批决策进缓冲区，返回读到的条数。"""
        entries = bus.read_decisions(self.client, self.cursor)
        for entry_id, fields in entries:
            self.cursor = entry_id
            item = parse_item(fields)
            if item.get("pushed") in (1, True, "1", "true"):
                self.batch.append(item)
            elif self.include_blocked:
                self.blocked.append(item)
        return len(entries)

    def due(self) -> bool:
        return bool(self.batch) and self.clock() - self.started >= self.flush

    def flush_batch(self) -> bool:
        """发一条摘要消息并清空缓冲；没内容就不发（空消息比不发更吵）。"""
        if not self.batch and not self.blocked:
            return False
        moment = datetime.fromtimestamp(self.now())
        # 日期在发的那一刻算，不在启动时算：隔夜常驻的进程不能把新的一天标成昨天。
        title, text = render(self.batch, self.blocked,
                             moment.strftime("%Y-%m-%d"), moment, cap=self.cap)
        ok = self.sender(self.channel, self.url, self.secret, title, text)
        logger.info("告警批次%s：可执行 %d 条、拦截 %d 条", "成功" if ok else "失败",
                    len(self.batch), len(self.blocked))
        if ok:
            self.sent += 1
        else:
            self.failed += 1
        self.batch, self.blocked = [], []
        self.started = self.clock()
        return ok

    def run(self, rounds: int | None = None) -> None:
        done = 0
        while rounds is None or done < rounds:
            self.pump()
            if self.due() or (rounds is not None and done == rounds - 1):
                self.flush_batch()
            else:
                self.sleeper(min(1.0, max(0.05, self.flush / 4)))
            done += 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="盘中决策 → 钉钉/企业微信告警（只推送，不下单）")
    p.add_argument("--channel", choices=[DINGTALK, WECOM],
                   default=os.environ.get("ALERT_CHANNEL", DINGTALK))
    p.add_argument("--url", default=os.environ.get("ALERT_WEBHOOK_URL", ""))
    p.add_argument("--secret", default=os.environ.get("ALERT_WEBHOOK_SECRET", ""))
    p.add_argument("--channel-key", default=os.environ.get("ALERT_CHANNEL_KEY", ""),
                   help="管理页面里配的渠道 key；不填就用该类型第一条启用的渠道")
    p.add_argument("--flush", type=float, default=30.0, help="攒批秒数，0=每条立即发")
    p.add_argument("--no-blocked", action="store_true", help="摘要里不带被风控拦下的信号")
    p.add_argument("--replay", action="store_true",
                   help="启动就把流里的存量决策发一遍（默认只发本进程启动之后的）")
    p.add_argument("--max-lines", type=int, default=40,
                   help="每段最多列几条，超出的指回当日决策文件（消息长度有上限）")
    p.add_argument("--rounds", type=int, default=0, help="跑多少轮后退出，0=常驻")
    p.add_argument("--dry-run", action="store_true", help="只打印消息，不真发")
    p.add_argument("--host", default=bus.HOST)
    p.add_argument("--port", type=int, default=bus.PORT)
    p.add_argument("--db", type=int, default=bus.DB)
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.dry_run:
        sender = print_sender
    else:
        if not args.url:
            try:
                name, args.url, args.secret = resolve_webhook(args.channel, args.channel_key)
                logger.info("告警渠道取自管理页面：%s（%s，%s，加签密钥%s）", name, args.channel,
                            redacted(args.url), "已解出" if args.secret else "为空")
            except Exception as exc:  # noqa: BLE001 配置读不到就是没配好，全部指向同一条出路
                raise SystemExit(f"读不到页面配的{args.channel}渠道：{exc}\n"
                                 "可在页面「通知渠道」里配好，或设 ALERT_WEBHOOK_URL，"
                                 "或先用 --dry-run 看消息") from exc
        sender = send

    client = bus.connect(args.host, args.port, args.db)
    pusher = AlertPusher(client, channel=args.channel, url=args.url, secret=args.secret,
                         flush=args.flush, include_blocked=not args.no_blocked, sender=sender,
                         replay=args.replay, cap=args.max_lines)
    logger.info("告警进程启动：渠道=%s 攒批=%.0f 秒 带拦截说明=%s", args.channel, args.flush,
                not args.no_blocked)
    pusher.run(rounds=args.rounds or None)
    logger.info("退出：已发 %d 批，失败 %d 批", pusher.sent, pusher.failed)
    return 0


def print_sender(channel: str, url: str, secret: str, title: str, text: str) -> bool:
    """--dry-run：把消息原样打到 stdout（不发网络、不碰密钥）。"""
    print(f"—— {title} ——\n{text}")
    return True


if __name__ == "__main__":
    sys.exit(main())
