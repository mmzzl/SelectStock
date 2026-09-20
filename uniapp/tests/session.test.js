import test from "node:test";
import assert from "node:assert/strict";
import { setSession, loadSession, clearSession, isLoggedIn } from "../src/store/session.js";

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