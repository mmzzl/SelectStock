import test from "node:test";
import assert from "node:assert/strict";
import { resolveRoute, TAB_PATHS } from "../src/logic/guard.js";

test("未登录访问任一 Tab → 登录页", () => {
  for (const tab of TAB_PATHS) {
    assert.deepEqual(resolveRoute({ path: tab, loggedIn: false }), { redirect: "pages/login/index" });
  }
});

test("已登录访问登录页 → 默认首 Tab（持仓）", () => {
  assert.deepEqual(resolveRoute({ path: "pages/login/index", loggedIn: true }), { redirect: "pages/holdings/index" });
});

test("已登录访问 Tab → 放行", () => {
  assert.deepEqual(resolveRoute({ path: "pages/diag/index", loggedIn: true }), { ok: true });
});

test("密码过期(428) → 强制改密页，拦截优先", () => {
  assert.deepEqual(resolveRoute({ path: "pages/holdings/index", loggedIn: true, passwordExpired: true }),
    { redirect: "pages/mine/password" });
});

test("未知路径 → 404 兜底回首 Tab", () => {
  assert.deepEqual(resolveRoute({ path: "pages/unknown/x", loggedIn: true }), { redirect: "pages/holdings/index" });
});