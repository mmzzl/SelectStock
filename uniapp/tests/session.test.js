import test from "node:test";
import assert from "node:assert/strict";
import { setSession, loadSession, clearSession, isLoggedIn, setTokens } from "../src/store/session.js";

function withUniMock() {
  const store = new Map();
  globalThis.uni = {
    setStorageSync(k, v) { store.set(k, v); },
    getStorageSync(k) { return store.get(k) ?? ""; },
    removeStorageSync(k) { store.delete(k); },
  };
  return store;
}

test("setSession 写入 uni 持久化键", () => {
  const store = withUniMock();
  setSession({ access_token: "t1", refresh_token: "r1", user: { id: 7 } });
  assert.equal(store.get("ss_access_token"), "t1");
  assert.equal(store.get("ss_refresh_token"), "r1");
  assert.deepEqual(store.get("ss_me"), { id: 7 });
});

test("loadSession 从 uni 存储恢复", () => {
  const store = withUniMock();
  store.set("ss_access_token", "t2");
  store.set("ss_refresh_token", "r2");
  store.set("ss_me", { id: 8 });
  const s = loadSession();
  assert.equal(s.token, "t2");
  assert.equal(s.refresh, "r2");
  assert.equal(s.me.id, 8);
});

test("clearSession 清除 token 并清空存储", () => {
  const store = withUniMock();
  setSession({ access_token: "t3", refresh_token: "r3", user: { id: 9 } });
  clearSession();
  assert.equal(store.has("ss_access_token"), false);
  assert.equal(store.has("ss_refresh_token"), false);
  assert.equal(store.has("ss_me"), false);
});

test("isLoggedIn 依据 token 判定", () => {
  withUniMock();
  setSession({ access_token: "t4" });
  assert.equal(isLoggedIn(), true);
  clearSession();
  assert.equal(isLoggedIn(), false);
});

test("无 uni 环境按内存返回", () => {
  delete globalThis.uni;
  setSession({ access_token: "m1" });
  assert.equal(loadSession().token, "m1");
  assert.equal(isLoggedIn(), true);
  clearSession();
  assert.equal(isLoggedIn(), false);
});

// 401 续期只拿到两份令牌（{access_token, refresh_token}），没有 user。
// 直接走 setSession 会把内存里的 me 置空 ——「我的」页用户名当场变空，等于续个期丢一次身份。
test("setTokens 只换两份令牌，不动已存的 user", () => {
  withUniMock();
  setSession({ access_token: "t1", refresh_token: "r1", user: { id: 21, name: "llmm" } });
  setTokens({ access_token: "t2", refresh_token: "r2" });
  const s = loadSession();
  assert.equal(s.token, "t2");
  assert.equal(s.refresh, "r2");
  assert.deepEqual(s.me, { id: 21, name: "llmm" });
});

test("setTokens 不传 refresh 时保留旧 refresh（服务端没给就别自己清空）", () => {
  withUniMock();
  setSession({ access_token: "t1", refresh_token: "r1" });
  setTokens({ access_token: "t2" });
  assert.equal(loadSession().refresh, "r1");
});