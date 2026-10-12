// uniapp/tests/ws.test.js —— 假 socket 验证首帧鉴权/心跳/退避/latest_inbox_id 对齐
import test from "node:test";
import assert from "node:assert/strict";
import { createNotifySocket } from "../src/logic/ws.js";

class FakeSocket {
  constructor() { this.raw = []; this.sent = []; this.onopen = null; this.onmessage = null; this.onclose = null; }
  send(t) { this.raw.push(t); this.sent.push(JSON.parse(typeof t === "string" ? t : t.data)); }
  close() { this.onclose && this.onclose({}); }
}

// 手动推进的定时器：记住待跑回调，测试里自己 tick()
function fakeClock() {
  const clock = { planned: [], fn: null };
  clock.scheduler = (fn, ms) => { clock.planned.push(ms); clock.fn = fn; return clock.planned.length; };
  clock.unscheduler = () => { clock.fn = null; };
  clock.tick = () => clock.fn();
  return clock;
}

test("连上即发 auth 首帧", () => {
  let sock;
  const s = createNotifySocket({ url: "x", token: "T",
    socketFactory: () => (sock = new FakeSocket()), onMessage: () => {}, onReconnect: () => {} });
  s.connect();
  sock.onopen();
  assert.deepEqual(sock.sent[0], { type: "auth", token: "T" });
  s.close();
});

test("uni 端要按 SocketTask 口径包成 {data}，否则发出去的是 undefined", () => {
  let sock;
  const s = createNotifySocket({ url: "x", token: "T", objectPayload: true,
    socketFactory: () => (sock = new FakeSocket()), onMessage: () => {}, onReconnect: () => {} });
  s.connect(); sock.onopen();
  assert.deepEqual(JSON.parse(sock.raw[0].data), { type: "auth", token: "T" });
  s.close();
});

test("hello → 触发 onReconnect(since_id, unread)", () => {
  const seen = []; let sock;
  const s = createNotifySocket({ url: "x", token: "T",
    socketFactory: () => (sock = new FakeSocket()), onMessage: () => {},
    onReconnect: (info) => seen.push(info) });
  s.connect(); sock.onopen();
  sock.onmessage({ data: JSON.stringify({ type: "hello", latest_inbox_id: "m9", unread_count: 3 }) });
  assert.deepEqual(seen[0], { sinceId: "m9", unreadCount: 3 });
  s.close();
});

test("msg 帧回调 + 回 pong + 心跳 ping→pong", () => {
  const got = []; let sock;
  const s = createNotifySocket({ url: "x", token: "T",
    socketFactory: () => (sock = new FakeSocket()), onMessage: (m) => got.push(m), onReconnect: () => {} });
  s.connect(); sock.onopen();
  sock.onmessage({ data: JSON.stringify({ type: "msg", message: { id: "m1" } }) });
  sock.onmessage({ data: JSON.stringify({ type: "ping" }) });
  assert.equal(got.length, 1);
  assert.ok(sock.sent.filter((f) => f.type === "pong").length >= 2);
  s.close();
});

test("定时 ping 保活：服务端回 pong 就不判死", () => {
  const clock = fakeClock(); let sock;
  const s = createNotifySocket({ url: "x", token: "T", socketFactory: () => (sock = new FakeSocket()),
    onMessage: () => {}, onReconnect: () => {}, scheduler: clock.scheduler, unscheduler: clock.unscheduler });
  s.connect(); sock.onopen();
  assert.equal(clock.planned[0], 20000);          // 第一拍在连上之后排，间隔 20s
  clock.tick();
  assert.deepEqual(sock.sent.at(-1), { type: "ping" });
  sock.onmessage({ data: JSON.stringify({ type: "pong" }) });
  for (let i = 0; i < 6; i += 1) {                     // 每拍都有回应
    clock.tick();
    sock.onmessage({ data: JSON.stringify({ type: "pong" }) });
  }
  assert.equal(s.connected, true);
  assert.equal(clock.planned.filter((ms) => ms === 20000).length, 8);
  s.close();
});

test("连续没 pong → 主动断开重连，且旧 socket 迟到的 onclose 不再排第二次", () => {
  const clock = fakeClock(); let sock;
  const s = createNotifySocket({ url: "x", token: "T", socketFactory: () => (sock = new FakeSocket()),
    onMessage: () => {}, onReconnect: () => {}, heartbeat: { interval: 20000, tolerateMissed: 2 },
    scheduler: clock.scheduler, unscheduler: clock.unscheduler, backoff: { base: 1000, max: 30000 } });
  s.connect(); sock.onopen();
  clock.tick(); clock.tick();                      // 两拍没人应答
  clock.tick();                                    // 第三拍判死
  assert.equal(s.connected, false);
  const reconnectDelay = clock.planned.at(-1);
  assert.ok(reconnectDelay >= 500 && reconnectDelay <= 1500);
  sock.onclose({});                                // 旧 socket 迟到
  assert.equal(clock.planned.at(-1), reconnectDelay);   // 没有再排一次
  s.close();
});

test("退避：断线后 delay 递增且有上限", () => {
  const q = [];
  let sock;
  const s = createNotifySocket({ url: "x", token: "T", heartbeat: { interval: 1_000_000_000, tolerateMissed: 2 },
    socketFactory: () => (sock = new FakeSocket()), onMessage: () => {}, onReconnect: () => {},
    scheduler: (fn, ms) => { q.push({ fn, ms }); return q.length; }, unscheduler: () => {},
    backoff: { base: 1000, max: 30000 } });
  s.connect(); sock.onopen();
  const delays = [];
  for (let i = 0; i < 4; i += 1) {
    sock.onclose();                                 // 已排过重连时不会再排第二次
    const job = q.at(-1); delays.push(job.ms); job.fn();   // 立刻兑现，让次数往上走
  }
  assert.ok(delays[0] >= 500 && delays[0] <= 1500);
  assert.ok(delays[1] >= 1000 && delays[1] <= 3000);
  assert.ok(delays[2] >= 2000 && delays[2] <= 6000);
  assert.ok(delays.every((d) => d <= 30000));
  s.close();
});

test("用户 close 后不再心跳、不再重连", () => {
  const clock = fakeClock(); let sock;
  const s = createNotifySocket({ url: "x", token: "T", socketFactory: () => (sock = new FakeSocket()),
    onMessage: () => {}, onReconnect: () => {}, scheduler: clock.scheduler, unscheduler: clock.unscheduler });
  s.connect(); sock.onopen();
  const before = clock.planned.length;
  s.close();
  assert.equal(clock.fn, null);                    // 心跳已取消
  sock.onclose({});
  assert.equal(clock.planned.length, before);      // 关闭没引出重连
});
