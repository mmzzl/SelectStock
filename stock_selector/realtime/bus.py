"""realtime/bus.py — 行情上行的 redis 契约：最新价、tick 流、心跳、盘中指标。

链路形状（需求文档的 1/2 两条）：
    轮询服务 ──写──► rt:quote / rt:ticks ──读──► 盘中策略引擎 ──► 风控 ──► 告警
用 redis 而不是 Kafka/RabbitMQ：本机已有 redis 7.2.5 免密运行，单机链路上 stream 的
消费者组、MAXLEN 限长、ACK 语义够用，再多引一个中间件只增加运维面。

键约定（默认 db 5，和 celery 的 db 3、爬虫用的 db 0 隔开）：

| 键 | 类型 | 内容 |
|---|---|---|
| `rt:quote` | Hash(code → 快照 JSON) | 最新行情，策略引擎随手可查当前价，不必等下一拍 |
| `rt:ticks` | Stream | **只有变化才发**的 tick 流（MAXLEN 限长，防止内存无上限涨） |
| `rt:decisions` | Stream | 过完风控的最终决策，告警链路消费；被拦的也在里面（`verdict=block`） |
| `rt:heartbeat` | String(JSON) | 上一轮行情统计：轮数/命中/耗时/错误，运维据此判断轮询是否还活着 |
| `rt:engine-heartbeat` | String(JSON) | 上一轮引擎统计：读到多少 tick、评估多少只、出多少信号 |
| `rt:emitted:{日期}` | Set | 当天已经报过的 `代码|信号种类`：盘中告警「一票一信号一天只报一次」的去重表 |
| `rt:metric:{code}` | Hash | 盘中实时指标（均线/MACD/量比…），由策略引擎写、告警链路读 |

`rt:quote` 与 `rt:heartbeat` 都带 TTL：停服后不能让人读到「昨天的实时价」——行情数据
一旦过期就不是数据而是误导。
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

HOST, PORT, DB = "127.0.0.1", 6379, 5

KEY_QUOTE = "rt:quote"
KEY_TICKS = "rt:ticks"
KEY_DECISIONS = "rt:decisions"
KEY_HEARTBEAT = "rt:heartbeat"
KEY_ENGINE_HEARTBEAT = "rt:engine-heartbeat"
KEY_EMITTED_PREFIX = "rt:emitted:"
METRIC_PREFIX = "rt:metric:"

QUOTE_TTL = 4 * 3600          # 一个交易日 + 余量：隔天必读不到，也不会当天误过期
METRIC_TTL = 6 * 3600
EMIT_TTL = 18 * 3600          # 去重表键名已带日期，TTL 只是别让历史键堆在库里
DEFAULT_MAXLEN = 20000        # tick 流保留长度：约 2 万条变化，够复盘半个交易日
DECISION_MAXLEN = 5000        # 决策流短得多：一条决策背后是几十上百个 tick


def connect(host: str = HOST, port: int = PORT, db: int = DB) -> "object":
    """连 redis。连接失败直接抛——行情链路连不上中间件必须炸，不能静默空跑。"""
    import redis

    client = redis.Redis(host=host, port=port, db=db, decode_responses=True)
    client.ping()
    logger.info("redis 已连接 %s:%d/db%d", host, port, db)
    return client


def _dump(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def metric_key(code: str) -> str:
    return f"{METRIC_PREFIX}{code}"


def emitted_key(day: str) -> str:
    return f"{KEY_EMITTED_PREFIX}{day}"


def mark_once(client, day: str, key: str) -> bool:
    """当天第一次记这个键返回 True，重复返回 False —— 盘中告警的「一票一信号一天只报一次」。

    去重表必须落在 redis 而不是引擎进程里：重启不能把上午已经报过的信号重新报一遍。键名带
    日期，跨天天然错开（不必清理昨天的），所以这里只需要在建键时挂一个 TTL 防止堆积。
    """
    name = emitted_key(day)
    added = client.sadd(name, key)
    if added and client.ttl(name) < 0:        # -1=还没设过期，-2=刚被别的连接清掉
        client.expire(name, EMIT_TTL)
    return bool(added)


def put_quotes(client, quotes: dict[str, dict]) -> int:
    """写入最新行情（一次 pipeline：HSET 全量 + 续 TTL），返回写入只数。"""
    if not quotes:
        return 0
    pipe = client.pipeline()
    pipe.hset(KEY_QUOTE, mapping={code: _dump(q) for code, q in quotes.items()})
    pipe.expire(KEY_QUOTE, QUOTE_TTL)
    pipe.execute()
    return len(quotes)


def latest(client, codes: list[str] | None = None) -> dict[str, dict]:
    """读最新行情；不给 codes 就是全量。读不到的键不会出现在结果里（不补空快照）。"""
    raw = client.hgetall(KEY_QUOTE) if not codes else _multi(client, codes)
    out = {}
    for code, text in raw.items():
        if not text:
            continue
        try:
            out[str(code)] = json.loads(text)
        except ValueError:
            logger.warning("rt:quote 里 %s 不是合法 JSON，已跳过", code)
    return out


def _multi(client, codes: list[str]) -> dict[str, str]:
    values = client.hmget(KEY_QUOTE, list(codes))
    return {str(c): v for c, v in zip(codes, values) if v}


def emit_ticks(client, ticks: list[dict], maxlen: int = DEFAULT_MAXLEN) -> int:
    return emit_stream(client, KEY_TICKS, ticks, maxlen=maxlen)


def emit_stream(client, key: str, items: list[dict], maxlen: int = DEFAULT_MAXLEN) -> int:
    """追加事件流。`approximate=True` 让 MAXLEN 用 `~` 语义按整块裁剪，比精确裁剪便宜得多。"""
    if not items:
        return 0
    pipe = client.pipeline()
    for item in items:
        pipe.xadd(key, _flat(item), maxlen=maxlen, approximate=True)
    pipe.execute()
    return len(items)


def _text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _flat(item: dict) -> dict[str, str]:
    """stream field 只能是字符串：None 表示「这项没数」，直接不写而不是写 "None"。"""
    return {key: _text(value) for key, value in item.items() if value is not None}


def read_ticks(client, last_id: str = "0-0", count: int = 500) -> list[tuple[str, dict]]:
    return read_stream(client, KEY_TICKS, last_id, count)


def read_decisions(client, last_id: str = "0-0", count: int = 200) -> list[tuple[str, dict]]:
    return read_stream(client, KEY_DECISIONS, last_id, count)


def read_stream(client, key: str, last_id: str = "0-0", count: int = 500) -> list[tuple[str, dict]]:
    """消费者侧读法：返回 [(entry_id, fields)]，按 id 升序。id 原样带回去就是游标。"""
    entries = client.xrange(key, min=last_id, count=count)
    out = []
    for entry_id, fields in entries:
        if entry_id == last_id:          # xrange 的 min 是闭区间，跳过游标自身
            continue
        out.append((entry_id, dict(fields)))
    return out


def stream_len(client, key: str = KEY_TICKS) -> int:
    return int(client.xlen(key))


def last_id(client, key: str) -> str:
    """流里最后一个 entry 的 id（空流是 `0-0`）。

    新启动的消费者用它当起始游标：告警进程如果不从流尾开始，明天早上会把今天的决策
    原样重推一遍——过期的「可执行」比不发更糟。
    """
    entries = client.xrevrange(key, max="+", min="-", count=1)
    return str(entries[0][0]) if entries else "0-0"


def heartbeat(client, stats: dict, key: str = KEY_HEARTBEAT) -> None:
    """上报本轮统计（值里必须带 at，否则看不出是不是刚写的）。"""
    client.set(key, _dump(stats), ex=QUOTE_TTL)


def last_heartbeat(client, key: str = KEY_HEARTBEAT) -> dict:
    raw = client.get(key)
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def put_metrics(client, code: str, metrics: dict) -> None:
    """盘中指标写 `rt:metric:{code}`：策略引擎算一次，告警与前端都从这里读，不在界面重算。"""
    if not metrics:
        return
    pipe = client.pipeline()
    pipe.hset(metric_key(code), mapping={k: _text(v) for k, v in metrics.items() if v is not None})
    pipe.expire(metric_key(code), METRIC_TTL)
    pipe.execute()


def get_metrics(client, code: str) -> dict:
    return {k: v for k, v in (client.hgetall(metric_key(code)) or {}).items()}
