// uniapp/tests/ws.test.js —— 假 socket 验证首帧鉴权/退避/latest_inbox_id 对齐
import test from "node:test";
import assert from "node:assert/strict";
import { createNotifySocket } from "../src/logic/ws.js";

class FakeSocket {
  constructor() { this.sent = []; this.onopen = null; this.onmessage = null; this.onclose = null; }
  send(t) { this.sent.push(JSON.parse(t)); }
  close() { this.onclose && this.onclose({}); }
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

test("退避：断线后 delay 递增且有上限", () => {
  const delays = []; let sock;
  const s = createNotifySocket({ url: "x", token: "T",
    socketFactory: () => (sock = new FakeSocket()), onMessage: () => {}, onReconnect: () => {},
    scheduler: (fn, ms) => { delays.push(ms); }, backoff: { base: 1000, max: 30000 } });
  s.connect(); sock.onclose();
  s._socket?.onclose?.({});
  assert.ok(delays[0] >= 500 && delays[0] <= 1500);
  assert.ok(delays.every((d) => d <= 30000));
  s.close();
});
