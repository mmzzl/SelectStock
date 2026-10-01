// uniapp/tests/client.test.js —— 注入假适配器，验证 token/401/428/解包/raw/上传
import test from "node:test";
import assert from "node:assert/strict";
import { createClient, UnauthorizedError, PasswordExpiredError, BizError } from "../src/api/client.js";

function fakeAdapter() {
  const calls = [];
  return {
    calls,
    request(cfg) { calls.push(cfg); return Promise.resolve(this._next); },
    upload(cfg) { calls.push({ upload: true, ...cfg }); return Promise.resolve(this._next); },
    _next: { statusCode: 200, data: { code: 0, data: { items: [] }, message: "ok" } },
  };
}

test("成功：解包 data 并携带 Bearer", async () => {
  const a = fakeAdapter();
  const c = createClient({ adapter: a, getToken: () => "T1" });
  const out = await c.get("/api/v1/holdings", { query: { page: 1 } });
  assert.deepEqual(out, { items: [] });
  assert.equal(a.calls[0].header.Authorization, "Bearer T1");
  assert.match(a.calls[0].url, /\/api\/v1\/holdings\?page=1/);
});

test("401：清会话并抛 UnauthorizedError", async () => {
  const a = fakeAdapter();
  a._next = { statusCode: 401, data: { code: 401, message: "未认证" } };
  let cleared = false;
  const c = createClient({ adapter: a, getToken: () => "T1", onUnauthorized: () => { cleared = true; } });
  await assert.rejects(() => c.get("/api/v1/holdings"), (e) => e.name === "UnauthorizedError");
  assert.equal(cleared, true);
  assert.ok(UnauthorizedError);
});

test("428：抛 PasswordExpiredError（跳改密）", async () => {
  const a = fakeAdapter();
  a._next = { statusCode: 428, data: { code: 428, message: "密码已过期" } };
  const c = createClient({ adapter: a, getToken: () => "T1" });
  await assert.rejects(() => c.get("/api/v1/holdings"), (e) => e.name === "PasswordExpiredError");
  assert.ok(PasswordExpiredError);
});

test("业务失败 code!=0：抛 BizError 带 message", async () => {
  const a = fakeAdapter();
  a._next = { statusCode: 200, data: { code: 400, message: "持仓上限 200" } };
  const c = createClient({ adapter: a, getToken: () => "T1" });
  await assert.rejects(() => c.post("/api/v1/trades/import/commit", { body: {} }),
    (e) => e.name === "BizError" && /200/.test(e.message));
  assert.ok(BizError);
});

test("raw 文本（粘贴交割单）：字符串 body 原样透传 + text/plain", async () => {
  const a = fakeAdapter();
  a._next = { statusCode: 200, data: { code: 0, data: { total: 0, errors: [] }, message: "" } };
  const c = createClient({ adapter: a, getToken: () => "T1" });
  await c.request("POST", "/api/v1/trades/import/preview",
    { body: "成交日期,证券代码\n2026-09-01,600519", header: { "Content-Type": "text/plain" } });
  assert.equal(a.calls[0].data, "成交日期,证券代码\n2026-09-01,600519"); // 未 JSON.stringify
  assert.equal(a.calls[0].header["Content-Type"], "text/plain");
});

test("上传：multipart 走 adapter.upload 并携带 token，解包 data", async () => {
  const a = fakeAdapter();
  a._next = { statusCode: 200, data: { code: 0, data: { total: 2, errors: [] }, message: "" } };
  const c = createClient({ adapter: a, getToken: () => "T1" });
  const out = await c.upload("/api/v1/trades/import/preview", { filePath: "/tmp/a.csv", name: "file" });
  const call = a.calls[0];
  assert.ok(call.upload);
  assert.equal(call.url, "/api/v1/trades/import/preview");
  assert.equal(call.header.Authorization, "Bearer T1");
  assert.deepEqual(out, { total: 2, errors: [] });
});

test("上传返回字符串 body：JSON.parse 后解包", async () => {
  const a = fakeAdapter();
  a._next = { statusCode: 200, data: '{"code":0,"data":{"url":"/u/x.apk"},"message":""}' };
  const c = createClient({ adapter: a, getToken: () => "" });
  const out = await c.upload("/api/v1/upgrade/packages/upload", { filePath: "/tmp/x.apk" });
  assert.deepEqual(out, { url: "/u/x.apk" });
});

test("领域 API 路径映射（契约消费）", async () => {
  const a = fakeAdapter();
  const c = createClient({ adapter: a, getToken: () => "T" });
  const { holdingsApi } = await import("../src/api/holdings.js");
  const { tradesApi } = await import("../src/api/trades.js");
  const { selectionApi } = await import("../src/api/selection.js");
  const { messagesApi } = await import("../src/api/messages.js");
  const { upgradeApi } = await import("../src/api/upgrade.js");
  await holdingsApi(c).pnl({ natural_day: "2026-09-20" });
  assert.equal(a.calls[0].url, "/api/v1/holdings/pnl?natural_day=2026-09-20");
  await tradesApi(c).previewText("x");
  assert.equal(a.calls[1].url, "/api/v1/trades/import/preview");
  await selectionApi(c).run(["trend_rebound"]);
  assert.deepEqual(JSON.parse(JSON.stringify(a.calls[2].data)), { strategies: ["trend_rebound"] });
  await messagesApi(c).markAllRead();
  assert.equal(a.calls[3].method, "PUT");
  assert.equal(a.calls[3].url, "/api/v1/messages/read_all");
  await upgradeApi(c).latest({ platform: "h5", version_code: 1 });
  assert.equal(a.calls[4].url, "/api/app/latest?platform=h5&version_code=1");
});
test("App 端缺 URLSearchParams：空 query 不拼 ?，值自己编码", async () => {
  const a = fakeAdapter();
  const saved = globalThis.URLSearchParams;
  delete globalThis.URLSearchParams; // Android JS 核心没有该全局
  try {
    const { holdingsApi } = await import("../src/api/holdings.js");
    const api = holdingsApi(createClient({ adapter: a, getToken: () => "T" }));
    await api.list();
    assert.equal(a.calls[0].url, "/api/v1/holdings");
    await api.pnl({ natural_day: "2026-09-20", note: "a b&c", skip: undefined });
    assert.equal(a.calls[1].url, "/api/v1/holdings/pnl?natural_day=2026-09-20&note=a%20b%26c");
  } finally {
    globalThis.URLSearchParams = saved;
  }
});
