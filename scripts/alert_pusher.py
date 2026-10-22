"""告警推送：消费 redis `rt:decisions`，把过完风控的最终决策发到钉钉/企业微信机器人。

链路里「告警/交易模块」那一格，按需求**只推送消息，不接券商下单**。

四条设计约束：
- **一批一条消息 + 同票同方向当天只报第一次**：盘中决策是持续流，一条发一条会把群刷爆、也很
  容易撞到机器人的 20 条/分钟限流。攒 `--flush` 秒（默认 15 分钟）合成一条 markdown；同一只
  票同一方向当天只进第一条消息，后面的重复只计数。理由是 A 股 T+1 且资金有限——一天里对着
  同一只票喊八次「卖出」并不能多卖出一次，只会让人关掉群通知。决策本身一条没丢，全量在
  `data/intraday_decisions/<日期>.parquet`。
- **跌停/利空不等窗口**：`--instant` 里的种类一到就发，这类晚一分钟就出不了。
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
OPPOSITE = {"buy": "sell", "sell": "buy"}
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


def fmt_conflict(entry: dict) -> str:
    """冲突行：两个方向各是什么信号、各在几点几分，一条都不少——但不给任何下单量。"""
    sides = "／".join(f"{ACTION_LABEL.get(s['action'], s['action'])}：{s['label']}"
                      f"（{s['at']}，{s['price']}）" for s in entry["sides"])
    head = f"- {entry['name']} {entry['code']}｜{sides}"
    return "\n".join(part for part in (head, f"  {entry['note']}") if part)


def render(items: list[dict], blocked: list[dict], day: str, moment: datetime,
           cap: int = 40, merged: int = 0, today: int = 0,
           conflicts: list[dict] | None = None) -> tuple[str, str]:
    """一批决策 → (标题, markdown 正文)。放行、冲突、拦截分列，后两段必须带原因。

    `cap` 是每段最多列几条：全市场盘中一批可能上百条，而机器人对消息长度有上限——宁可截断
    并指回当日决策文件，也不要发出一条被平台拒收的长消息（标题里的条数仍是全量，不掩饰）。
    `merged`/`today` 是「同票同方向当天只报一次」归并掉的条数与当天累计只数：归并 must 可见，
    否则读的人会以为今天这只票就只动过一次。
    """
    conflicts = conflicts or []
    if items:
        title = f"[{day}] 盘中信号 {len(items)} 条"
    elif conflicts:
        title = f"[{day}] 本批 {len(conflicts)} 只同日买卖冲突，先不给下单建议"
    else:
        title = f"[{day}] 本批 {len(blocked)} 条信号全部被风控拦下"
    lines = [f"**{moment:%H:%M:%S}** 更新"]
    if today:
        lines.append(f"当天累计已推 {today} 只（同票同方向只报第一次，重复的已归并）")
    if not items and not conflicts:
        lines.append("本批没有可执行信号。")
    if items:
        lines += ["", "### 可执行", *[fmt_line(item) for item in items[:cap]]]
        if len(items) > cap:
            lines.append(f"- 其余 {len(items) - cap} 条见 data/intraday_decisions/{day}.parquet")
    if conflicts:
        lines += ["", "### 信号冲突（同一只票当天既买又卖，两条都不给下单建议）",
                  *[fmt_conflict(entry) for entry in conflicts[:cap]]]
        if len(conflicts) > cap:
            lines.append(f"- 另有 {len(conflicts) - cap} 只同样冲突，明细在当日决策文件")
    if blocked:
        lines += ["", "### 风控已拦截（不给下单建议，原因如下）",
                  *[fmt_line(item, blocked=True) for item in blocked[:cap]]]
        if len(blocked) > cap:
            lines.append(f"- 另有 {len(blocked) - cap} 条同样被拦下，明细在当日决策文件")
    if merged:
        lines += ["", f"（另有 {merged} 条同票同方向的重复信号，今天这只票已报过，不再刷屏）"]
    return title, "\n".join(lines)


def parse_item(fields: dict) -> dict:
    """stream 里全是字符串：数值列转回来，`metrics` 还原成字典。

    `code` 例外，必须留字符串并补齐 6 位：`int("000001")` 会把深市代码的前导零吃掉，
    消息里就成了「平安银行 1」，用户按这个数字去下单是要出事的。
    """
    out = {}
    for key, value in fields.items():
        if key == "metrics":
            try:
                out[key] = json.loads(value)
            except ValueError:
                out[key] = {}
            continue
        if key == "code":
            out[key] = str(value).zfill(6)
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

    两层降噪（都是「同一件事只说一次」，不是把信号藏起来）：
    - **当日归并**：同一只票同一方向当天只进第一条消息，后面重复的只计数。引擎那侧的去重
      键是「票+信号种类」，而一只票可以同时命中死叉、跌破支撑、托单好几种，票级看就是刷屏；
      决策照样全量落 parquet，一条没丢。
    - **同日反向 = 冲突**：同一只票当天又给买点又给卖点时，两条一起撤下「可执行」，改列一段
      「信号冲突」。风控的 `family_flip` 只拦「同一个指标几十秒内翻面」，剩下的两头（不同指标
      各说一面、反方向落在下一批）只能在这里兜——上一条要是已经发出去了，消息撤不回来，
      就把话说清：那批已经推过卖出，这条买入不再给下单建议。
    - **攒批窗口**：`flush` 秒内的一批合成一条。窗口从**本批第一条**到达时起算，否则常驻
      空闲会让「窗口」永远在早上就已到期、下一条一来立刻发。
    - **即时通道**：`instant` 里的种类（默认跌停、利空事件）一到就发，不等窗口——这类恰恰
      是「晚一分钟就出不了」的那种，攒批对它们没有意义。
    """

    def __init__(self, client, *, channel=DINGTALK, url="", secret="", flush=900.0,
                 include_blocked=True, sender=send, now=time.time, clock=time.monotonic,
                 sleeper=time.sleep, replay=False, cap=40,
                 instant=("limit_down_touch", "event_bearish")):
        self.client = client
        self.channel, self.url, self.secret = channel, url, secret
        self.flush = flush
        self.include_blocked = include_blocked
        self.cap = cap
        self.instant = set(instant or ())
        self.sender = sender
        self.now = now
        self.clock = clock
        self.sleeper = sleeper
        self.cursor = "0-0" if replay else bus.last_id(client, bus.KEY_DECISIONS)
        self.batch: list[dict] = []
        self.blocked: list[dict] = []
        self.window: float | None = None        # 本批第一条到达的时刻；None = 批是空的
        self.day = ""
        self._seen: dict[tuple, dict] = {}     # 当日已进过消息的 (代码, 方向) → 那条决策
        self._seen_block: set[tuple] = set()     # 当日已说过的 (代码, 拦截规则)
        self.merged = 0                         # 本批被归并掉的条数
        self.merged_block = 0
        self.pushed_today = 0                   # 当日已推的可执行只数（标题外的累计口径）
        self.urgent = False
        self.sent = 0
        self.failed = 0

    def _roll(self, day: str) -> None:
        """换日就把当日归并表清空：昨天的「报过一次」管不到今天。"""
        if day and day != self.day:
            self.day = day
            self._seen, self._seen_block = {}, set()
            self.merged = self.merged_block = self.pushed_today = 0

    def pump(self) -> int:
        """读一批决策进缓冲区，返回**新进缓冲**的条数（当日重复的已归并，不计）。"""
        entries = bus.read_decisions(self.client, self.cursor)
        kept = 0
        for entry_id, fields in entries:
            self.cursor = entry_id
            item = parse_item(fields)
            self._roll(str(item.get("day") or ""))
            allowed = item.get("pushed") in (1, True, "1", "true")
            key = ((item.get("code"), item.get("action")) if allowed
                   else (item.get("code"), item.get("blocked_by")))
            if allowed:
                if key in self._seen:
                    self.merged += 1
                    continue
                self._seen[key] = item
                self.pushed_today += 1
                if item.get("kind") in self.instant:
                    self.urgent = True
            else:
                if not self.include_blocked:
                    continue
                if key in self._seen_block:
                    self.merged_block += 1
                    continue
                self._seen_block.add(key)
            if self.window is None:
                self.window = self.clock()
            (self.batch if allowed else self.blocked).append(item)
            kept += 1
        return kept

    def _conflict_entry(self, code: str, pair: dict[str, dict], fresh: set[str]) -> dict:
        """把两个方向拼成一行冲突：按时间排，并说明哪一头属于已经发出去的那批。"""
        items = sorted((pair[a] for a in ("buy", "sell") if a in pair),
                       key=lambda item: item.get("ts") or 0)

        def at(item: dict) -> str:
            return datetime.fromtimestamp(item.get("ts") or self.now()).strftime("%H:%M:%S")

        stale = next((pair[a] for a in pair if a not in fresh), None)
        if stale is None:
            note = ("同一批里两个方向都给到，说明指标在来回摆而不是出了新信息；"
                    "两条都不当下单依据，先别看这一只")
        else:
            note = (f"{at(stale)} 那批已推过{ACTION_LABEL.get(stale.get('action'), '另一条')}"
                    f"（{KIND_LABEL.get(stale.get('kind'), '-')}），这条反向不再给下单建议；"
                    "两条都别按")
        return {"code": code, "name": (items[0].get("name") or "").strip() or code,
                "sides": [{"action": item.get("action"),
                           "label": KIND_LABEL.get(item.get("kind"), item.get("kind") or "-"),
                           "price": item.get("signal_price"), "at": at(item)} for item in items],
                "note": note}

    def _split_conflicts(self) -> list[dict]:
        """把本批里「同一只票正反两面都给了建议」的票摘出来，返回冲突行，剩下的留在 `self.batch`。"""
        sides_by_code: dict[str, dict[str, dict]] = {}
        for item in self.batch:
            sides_by_code.setdefault(str(item.get("code")), {})[item.get("action")] = item
        conflicts = []
        for code, fresh in sides_by_code.items():
            # 反面不在本批、但当天已经推出去过 → 也算冲突：那一条撤不回来，只能把话说清
            missing = {OPPOSITE[a] for a in fresh if OPPOSITE.get(a)} - set(fresh)
            prior = {a: self._seen[(code, a)] for a in missing if (code, a) in self._seen}
            if len(fresh) < 2 and not prior:
                continue
            conflicts.append(self._conflict_entry(code, {**fresh, **prior}, set(fresh)))
            self.pushed_today -= len(fresh)
        if conflicts:
            self.batch = [item for item in self.batch
                          if str(item.get("code")) not in {c["code"] for c in conflicts}]
        return conflicts

    def due(self) -> bool:
        return (self.window is not None
                and self.clock() - self.window >= self.flush)

    def flush_batch(self) -> bool:
        """发一条摘要消息并清空缓冲；没内容就不发（空消息比不发更吵）。"""
        if not self.batch and not self.blocked:
            return False
        conflicts = self._split_conflicts()
        moment = datetime.fromtimestamp(self.now())
        # 日期在发的那一刻算，不在启动时算：隔夜常驻的进程不能把新的一天标成昨天。
        title, text = render(self.batch, self.blocked,
                             moment.strftime("%Y-%m-%d"), moment, cap=self.cap,
                             merged=self.merged + self.merged_block,
                             today=self.pushed_today, conflicts=conflicts)
        ok = self.sender(self.channel, self.url, self.secret, title, text)
        logger.info("告警批次%s：可执行 %d 条、冲突 %d 只、拦截 %d 条、归并 %d 条",
                    "成功" if ok else "失败", len(self.batch), len(conflicts),
                    len(self.blocked), self.merged + self.merged_block)
        if ok:
            self.sent += 1
        else:
            self.failed += 1
        self.batch, self.blocked = [], []
        self.window, self.urgent = None, False
        self.merged = self.merged_block = 0
        return ok

    def run(self, rounds: int | None = None) -> None:
        done = 0
        while rounds is None or done < rounds:
            self.pump()
            # 即时通道优先：跌停/利空这类等到窗口末期才发就等于没发。
            if self.urgent or self.due() or (rounds is not None and done == rounds - 1):
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
    p.add_argument("--flush", type=float, default=900.0,
                   help="攒批秒数（默认 15 分钟；0=每条立即发）")
    p.add_argument("--instant", default="limit_down_touch,event_bearish",
                   help="这些种类的决策一到就立即发送，不等攒批窗口（空串=全部按窗口攒）")
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
                         replay=args.replay, cap=args.max_lines,
                         instant=[k.strip() for k in args.instant.split(",") if k.strip()])
    logger.info("告警进程启动：渠道=%s 攒批=%.0f 秒 带拦截说明=%s 即时种类=%s", args.channel,
                args.flush, not args.no_blocked, args.instant or "无")
    pusher.run(rounds=args.rounds or None)
    logger.info("退出：已发 %d 批，失败 %d 批", pusher.sent, pusher.failed)
    return 0


def print_sender(channel: str, url: str, secret: str, title: str, text: str) -> bool:
    """--dry-run：把消息原样打到 stdout（不发网络、不碰密钥）。"""
    print(f"—— {title} ——\n{text}")
    return True


if __name__ == "__main__":
    sys.exit(main())
