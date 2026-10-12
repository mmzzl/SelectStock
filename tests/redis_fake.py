"""redis 的内存假客户端：实时链路与盘中引擎的测试共用。

不连真服务（测试不该依赖一个可能被别人占用的 6379），但键行为要真的记账：hash 的
field 覆盖、stream 的 id 递增与 MAXLEN 裁剪、TTL 存没存，都是被测代码真正依赖的性质。
断言的是「写了什么」，而不是「redis 怎么实现」。
"""

from __future__ import annotations


class FakePipe:
    def __init__(self, client):
        self.client = client
        self.calls = []

    def hset(self, key, mapping):
        self.calls.append(("hset", key, mapping))

    def expire(self, key, ttl):
        self.calls.append(("expire", key, ttl))

    def xadd(self, key, fields, maxlen=None, approximate=False):
        self.calls.append(("xadd", key, fields, maxlen, approximate))

    def execute(self):
        out = []
        for call in self.calls:
            if call[0] == "hset":
                out.append(self.client.hset(call[1], mapping=call[2]))
            elif call[0] == "expire":
                self.client.ttls[call[1]] = call[2]
                out.append(True)
            elif call[0] == "xadd":
                out.append(self.client.xadd(call[1], call[2], maxlen=call[3]))
            else:
                out.append(self.client.set(call[1], call[2], ex=call[3]))
        self.calls = []
        return out


class FakeRedis:
    """够用的 redis 记账：hash / stream / string / set 四种结构 + TTL 表。"""

    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = {}
        self.streams: dict[str, list[tuple[str, dict]]] = {}
        self.strings: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}
        self.ttls: dict[str, int] = {}
        self._seq = 0

    def pipeline(self):
        return FakePipe(self)

    def hset(self, key, field=None, value=None, mapping=None):
        bucket = self.hashes.setdefault(key, {})
        if mapping:
            bucket.update(mapping)
        elif field is not None:
            bucket[field] = value
        return len(bucket)

    def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hdel(self, key, *fields):
        bucket = self.hashes.get(key, {})
        return sum(1 for field in fields if bucket.pop(str(field), None) is not None)

    def hmget(self, key, fields):
        bucket = self.hashes.get(key, {})
        return [bucket.get(str(f)) for f in fields]

    def hlen(self, key):
        return len(self.hashes.get(key, {}))

    def set(self, key, value, ex=None):
        self.strings[key] = value
        if ex:
            self.ttls[key] = ex
        return True

    def get(self, key):
        return self.strings.get(key)

    def xadd(self, key, fields, maxlen=None):
        self._seq += 1
        entry_id = f"{self._seq}-0"
        stream = self.streams.setdefault(key, [])
        stream.append((entry_id, dict(fields)))
        if maxlen and len(stream) > maxlen:
            del stream[:len(stream) - maxlen]
        return entry_id

    def xrange(self, key, min="0-0", count=None):
        stream = self.streams.get(key, [])
        seq = int(str(min).split("-")[0])
        out = [(eid, fields) for eid, fields in stream if int(eid.split("-")[0]) >= seq]
        return out[:count] if count else out

    def xrevrange(self, key, max="+", min="-", count=None):
        stream = list(reversed(self.streams.get(key, [])))
        return stream[:count] if count else stream

    def xlen(self, key):
        return len(self.streams.get(key, []))

    def sadd(self, key, *values):
        bucket = self.sets.setdefault(key, set())
        before = len(bucket)
        bucket.update(str(v) for v in values)
        return len(bucket) - before

    def smembers(self, key):
        return set(self.sets.get(key, set()))

    def expire(self, key, ttl):
        self.ttls[key] = ttl
        return True

    def ttl(self, key):
        # 真 redis：-1 是键在但没设过期，-2 是键不存在。假客户端只需保证「小于 0」这一判定成立。
        return self.ttls.get(key, -1)
