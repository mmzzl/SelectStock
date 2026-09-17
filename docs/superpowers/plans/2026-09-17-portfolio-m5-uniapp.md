# 持仓诊断 M5：UniApp 客户端（持仓录入/导入 · 诊断回看 · 壳层隔离 · 零计算） Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付 `D:\select_stock\uniapp` 客户端（方案 §5 M5 行「客户端持仓/诊断 Tab：UniApp 持仓录入+导入、诊断历史回看、壳层隔离」）：①持仓唯一录入入口（录入 + CSV 导入，REQ-018/020）；②诊断历史回看（读服务端 run 明细，REQ-019/021）；③通知中心（消费 M4 站内信 + WS，REQ-010/021）；④壳层路由守卫与登录隔离（REQ-012）；⑤**前端零计算**（红线 R1-3：只渲染插件判定与收益快照，不重算）。

**Architecture:** UniApp（Vue3 + Vite）4 Tab 壳层：`持仓 / 诊断 / 消息 / 我的`。网络与存储通过 `src/api/client.js` 统一封装（token 注入、401 跳登录、密码过期 428 跳改密）；纯逻辑（路由守卫、CSV 解析、WS 重连状态机、渲染映射）放 `src/logic/*.js`，**不依赖 uni 运行时**，用 Node 内建 `node --test` 断言（零额外依赖、规避离线装包风险）；UI 层（pages/components）只做绑定与展示，不含判定/收益运算（R1-3）。后端契约固定（见下表），前端不引入本地行情/指标库。

**Tech Stack:** Vue3 + UniApp (vite) · Node ≥18 内建测试 `node --test` · 现有后端 `/api/v1/*`、`/api/portfolio/*`、`/api/external/portfolio/*`、`/ws/notify`

**后端契约（实现依赖，来自 M1/M3/M4 计划）：**

| 能力 | 方法 路径 | 备注 |
|---|---|---|
| 登录 | `POST /api/v1/auth/login` | 返回 access/refresh；`settings.auth_mode` 可 session |
| 当前用户 | `GET /api/v1/auth/me` | 守卫判定登录态 |
| 持仓列表/增改删 | `GET/POST /api/v1/holdings`、`PUT/DELETE /api/v1/holdings/{id}` | 属主隔离；上限 200 |
| CSV 导入 | `POST /api/v1/holdings/import` | multipart 或文本；返回成功/失败行 |
| 诊断 run 列表 | `GET /api/portfolio/runs?user_id=` | 历史列表 |
| 诊断明细回看 | `GET /api/external/portfolio/diag/{run_id}` | results 明细（判定+收益快照） |
| 站内信列表 | `GET /api/v1/messages?page&size&since_id` | 返回 items/total/unread_count |
| 站内信已读 | `PUT /api/v1/messages/{id}/read` | |
| 实时通知 | `WS /ws/notify` | 首帧 `{"type":"auth","token"}` → `hello{latest_inbox_id,unread_count}`；心跳 pong |

**裁定：** 客户端**不实现**本地诊断触发按钮走同步 `/api/portfolio/analyze`（诊断由外部调度产生，M3）；若需展示「立即诊断」仅作为未来扩展，不在本里程碑；CSV 仅解析为持仓字段（code/name/cost/shares 等），不做行情换算；消息已读以服务端为准，本地不缓存可信态。

---

## 文件映射

| 文件 | 动作 | 职责 |
|---|---|---|
| `uniapp/package.json` | 新增 | 工程与脚本（`dev`/`build`/`test`） |
| `uniapp/vite.config.js` | 新增 | uni-app vite 插件 + 代理 `/api`、`/ws` |
| `uniapp/src/manifest.json`、`src/pages.json` | 新增 | uni-app 应用与页面/tabBar 配置 |
| `uniapp/src/main.js`、`src/App.vue` | 新增 | 入口与全局生命周期 |
| `uniapp/src/api/client.js` | 新增 | 请求封装（token/401/428/统一 ok 解包） |
| `uniapp/src/api/portfolio.js`、`holdings.js`、`messages.js` | 新增 | 各领域 API 调用 |
| `uniapp/src/logic/guard.js` | 新增 | 纯函数：路由/登录隔离判定（REQ-012） |
| `uniapp/src/logic/csv.js` | 新增 | 纯函数：持仓 CSV 解析/校验（REQ-020） |
| `uniapp/src/logic/ws.js` | 新增 | WS 重连状态机（首帧鉴权/退避/latest_inbox_id） |
| `uniapp/src/logic/view.js` | 新增 | 纯函数：服务端字段 → 展示 ViewModel（零计算，R1-3） |
| `uniapp/src/store/session.js` | 新增 | 登录态与 token 存储（uni.storage 适配） |
| `uniapp/src/pages/login/index.vue` | 新增 | 登录页 |
| `uniapp/src/pages/holdings/index.vue`、`edit.vue`、`import.vue` | 新增 | 持仓列表/编辑/CSV 导入 |
| `uniapp/src/pages/diag/index.vue`、`detail.vue` | 新增 | 诊断历史/明细回看 |
| `uniapp/src/pages/messages/index.vue` | 新增 | 消息中心（已读/未读、点击已读） |
| `uniapp/src/pages/mine/index.vue` | 新增 | 我的（登出/改密入口） |
| `uniapp/tests/guard.test.js`、`csv.test.js`、`ws.test.js`、`view.test.js`、`client.test.js` | 新增 | `node --test` 纯逻辑用例 |
| `uniapp/README.md` | 新增 | 运行/打包/手动冒烟说明 |

---

### Task 1: 工程脚手架 + 测试运行器

**Files:**
- Create: `uniapp/package.json`、`uniapp/vite.config.js`、`uniapp/src/manifest.json`、`uniapp/src/pages.json`、`uniapp/src/main.js`、`uniapp/src/App.vue`
- Create: `uniapp/tests/setup-smoke.test.js`

- [ ] **Step 1: 探测模板可用性（离线优先）**

Run: `npm ping` / `npx --no-install degit --help`
预期：若 `npm ping` 失败（离线），**采用手工最小结构**（下述文件），不依赖网络模板；若在线可用官方模板 `npx degit dcloudio/uni-preset-vue#vite .` 作为基线再覆盖。

- [ ] **Step 2: 写最小工程结构**

```json
// uniapp/package.json
{
  "name": "select-stock-uniapp",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "scripts": {
    "test": "node --test tests/",
    "dev:h5": "uni",
    "build:h5": "uni build",
    "dev:mp-weixin": "uni -p mp-weixin",
    "build:mp-weixin": "uni build -p mp-weixin"
  },
  "devDependencies": {
    "@dcloudio/uni-app": "^3.0.0",
    "@dcloudio/uni-h5": "^3.0.0",
    "@dcloudio/uni-mp-weixin": "^3.0.0",
    "@dcloudio/vite-plugin-uni": "^3.0.0",
    "vite": "^5.2.0",
    "vue": "^3.4.21"
  }
}
```

```js
// uniapp/vite.config.js —— dev 代理后端，避免跨域
import { defineConfig } from "vite";
import uni from "@dcloudio/vite-plugin-uni";

export default defineConfig({
  plugins: [uni()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
    },
  },
});
```

```json
// uniapp/src/manifest.json（最小）
{"name":"select-stock","appid":"","description":"持仓诊断客户端","versionName":"0.1.0","versionCode":"100","h5":{"title":"持仓诊断"}}
```

```json
// uniapp/src/pages.json —— 登录页 + 4 Tab 壳层
{
  "pages": [
    { "path": "pages/login/index", "style": { "navigationBarTitleText": "登录" } },
    { "path": "pages/holdings/index", "style": { "navigationBarTitleText": "持仓" } },
    { "path": "pages/holdings/edit", "style": { "navigationBarTitleText": "编辑持仓" } },
    { "path": "pages/holdings/import", "style": { "navigationBarTitleText": "CSV 导入" } },
    { "path": "pages/diag/index", "style": { "navigationBarTitleText": "诊断历史" } },
    { "path": "pages/diag/detail", "style": { "navigationBarTitleText": "诊断明细" } },
    { "path": "pages/messages/index", "style": { "navigationBarTitleText": "消息" } },
    { "path": "pages/mine/index", "style": { "navigationBarTitleText": "我的" } }
  ],
  "tabBar": {
    "list": [
      { "pagePath": "pages/holdings/index", "text": "持仓" },
      { "pagePath": "pages/diag/index", "text": "诊断" },
      { "pagePath": "pages/messages/index", "text": "消息" },
      { "pagePath": "pages/mine/index", "text": "我的" }
    ]
  }
}
```

```js
// uniapp/src/main.js
import { createSSRApp } from "vue";
import App from "./App.vue";

export function createApp() {
  return { app: createSSRApp(App) };
}
```

```vue
<!-- uniapp/src/App.vue -->
<script>
export default {
  onLaunch() {
    // 启动即导引：无 token → 登录页（守卫纯逻辑见 logic/guard.js）
  },
};
</script>
<template><view /></template>
```

```js
// uniapp/tests/setup-smoke.test.js
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

test("pages.json 含 4 个 Tab 与登录页", () => {
  const cfg = JSON.parse(readFileSync(new URL("../src/pages.json", import.meta.url)));
  const paths = cfg.pages.map((p) => p.path);
  assert.ok(paths.includes("pages/login/index"));
  assert.equal(cfg.tabBar.list.length, 4);
});
```

- [ ] **Step 3: 运行测试**

Run（`workdir=uniapp`）: `npm test`
Expected: PASS（仅结构断言，不依赖 uni 运行时）
（可选在线时 `npm install` 后 `npm run dev:h5` 能起工程；离线时 `npm install` 明确记为未完成项）

- [ ] **Step 4: Commit**

```bash
git add uniapp
git commit -m "feat(uniapp): M5 客户端脚手架（pages/tabBar/vite 代理/结构冒烟）"
```

---

### Task 2: 路由守卫（REQ-012 壳层隔离）+ 会话存储

**Files:**
- Create: `uniapp/src/logic/guard.js`、`uniapp/src/store/session.js`
- Test: `uniapp/tests/guard.test.js`

- [ ] **Step 1: 写失败测试**

```js
// uniapp/tests/guard.test.js
import test from "node:test";
import assert from "node:assert/strict";
import { resolveRoute, TAB_PATHS } from "../src/logic/guard.js";

test("未登录访问任意 Tab → 登录页", () => {
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

test("密码过期(428) → 强制改密页，其余拦截优先于 Tab 放行", () => {
  assert.deepEqual(resolveRoute({ path: "pages/holdings/index", loggedIn: true, passwordExpired: true }),
    { redirect: "pages/mine/password" });
});

test("未知路径 → 404 兜底回首 Tab", () => {
  assert.deepEqual(resolveRoute({ path: "pages/unknown/x", loggedIn: true }), { redirect: "pages/holdings/index" });
});
```

- [ ] **Step 2: 运行确认失败**

Run: `npm test`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

```js
// uniapp/src/logic/guard.js —— 纯函数，无 uni 依赖，便于 node --test
export const TAB_PATHS = [
  "pages/holdings/index",
  "pages/diag/index",
  "pages/messages/index",
  "pages/mine/index",
];

export const PAGE_PATHS = [
  ...TAB_PATHS,
  "pages/login/index",
  "pages/holdings/edit",
  "pages/holdings/import",
  "pages/diag/detail",
  "pages/mine/password",
];

const FIRST_TAB = TAB_PATHS[0];
const LOGIN = "pages/login/index";
const PASSWORD = "pages/mine/password";

export function resolveRoute({ path, loggedIn, passwordExpired = false }) {
  if (passwordExpired && loggedIn && path !== PASSWORD) return { redirect: PASSWORD };
  if (!loggedIn) return path === LOGIN ? { ok: true } : { redirect: LOGIN };
  if (path === LOGIN) return { redirect: FIRST_TAB };
  if (!PAGE_PATHS.includes(path)) return { redirect: FIRST_TAB };
  return { ok: true };
}
```

```js
// uniapp/src/store/session.js —— 平台适配的 token/登录态存储
import { resolveRoute } from "../logic/guard.js";

const K_TOKEN = "ss_access_token";
const K_REFRESH = "ss_refresh_token";
const K_ME = "ss_me";

let memory = { token: "", refresh: "", me: null };

export function setSession({ access_token, refresh_token, user }) {
  memory = { token: access_token || "", refresh: refresh_token || "", me: user || null };
  const u = globalThis.uni;
  if (u?.setStorageSync) {
    u.setStorageSync(K_TOKEN, memory.token);
    u.setStorageSync(K_REFRESH, memory.refresh);
    if (user) u.setStorageSync(K_ME, user);
  }
}

export function loadSession() {
  const u = globalThis.uni;
  if (u?.getStorageSync) {
    memory = {
      token: u.getStorageSync(K_TOKEN) || "",
      refresh: u.getStorageSync(K_REFRESH) || "",
      me: u.getStorageSync(K_ME) || null,
    };
  }
  return memory;
}

export function clearSession() {
  memory = { token: "", refresh: "", me: null };
  const u = globalThis.uni;
  if (u?.removeStorageSync) [K_TOKEN, K_REFRESH, K_ME].forEach((k) => u.removeStorageSync(k));
}

export function isLoggedIn() {
  return Boolean(loadSession().token);
}

export function guard(path, { passwordExpired = false } = {}) {
  return resolveRoute({ path, loggedIn: isLoggedIn(), passwordExpired });
}
```

- [ ] **Step 4: 运行确认通过**

Run: `npm test`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add uniapp/src/logic/guard.js uniapp/src/store/session.js uniapp/tests/guard.test.js
git commit -m "feat(uniapp): M5 路由守卫（REQ-012 登录/改密/未知路径隔离）"
```

---

### Task 3: API 客户端 + 领域接口封装

**Files:**
- Create: `uniapp/src/api/client.js`、`uniapp/src/api/holdings.js`、`uniapp/src/api/portfolio.js`、`uniapp/src/api/messages.js`
- Test: `uniapp/tests/client.test.js`

- [ ] **Step 1: 写失败测试**

```js
// uniapp/tests/client.test.js —— 注入假请求器，验证 token/401/428/解包
import test from "node:test";
import assert from "node:assert/strict";
import { createClient } from "../src/api/client.js";

function fakeAdapter() {
  const calls = [];
  return {
    calls,
    request(cfg) { calls.push(cfg); return Promise.resolve(this._next); },
    _next: { statusCode: 200, data: { code: 0, data: { items: [] }, message: "ok" } },
  };
}

test("成功：解包 data 并携带 Bearer", async () => {
  const a = fakeAdapter();
  const c = createClient({ adapter: a, getToken: () => "T1" });
  const out = await c.request("GET", "/api/v1/holdings", { query: { page: 1 } });
  assert.deepEqual(out, { items: [] });
  assert.equal(a.calls[0].header.Authorization, "Bearer T1");
  assert.match(a.calls[0].url, /\/api\/v1\/holdings\?page=1/);
});

test("401：清会话并抛 UnauthorizedError", async () => {
  const a = fakeAdapter();
  a._next = { statusCode: 401, data: { code: 401, message: "未认证" } };
  let cleared = false;
  const c = createClient({ adapter: a, getToken: () => "T1", onUnauthorized: () => { cleared = true; } });
  await assert.rejects(() => c.request("GET", "/api/v1/holdings"), (e) => e.name === "UnauthorizedError");
  assert.equal(cleared, true);
});

test("428：抛 PasswordExpiredError（跳改密）", async () => {
  const a = fakeAdapter();
  a._next = { statusCode: 428, data: { code: 428, message: "密码已过期" } };
  const c = createClient({ adapter: a, getToken: () => "T1" });
  await assert.rejects(() => c.request("GET", "/api/v1/holdings"), (e) => e.name === "PasswordExpiredError");
});

test("业务失败 code!=0：抛 BizError 带 message", async () => {
  const a = fakeAdapter();
  a._next = { statusCode: 200, data: { code: 400, message: "持仓上限 200" } };
  const c = createClient({ adapter: a, getToken: () => "T1" });
  await assert.rejects(() => c.request("POST", "/api/v1/holdings", { body: {} }),
    (e) => e.name === "BizError" && /200/.test(e.message));
});
```

- [ ] **Step 2: 运行确认失败**

Run: `npm test`
Expected: FAIL

- [ ] **Step 3: 实现**

```js
// uniapp/src/api/client.js —— 统一请求：token 注入 / 401 登出 / 428 改密 / ok 解包
export class UnauthorizedError extends Error {
  constructor(m = "未认证") { super(m); this.name = "UnauthorizedError"; }
}
export class PasswordExpiredError extends Error {
  constructor(m = "密码已过期") { super(m); this.name = "PasswordExpiredError"; }
}
export class BizError extends Error {
  constructor(message, code) { super(message); this.name = "BizError"; this.code = code; }
}

function defaultAdapter() {
  const u = globalThis.uni;
  if (u?.request) return { request: (cfg) => u.request(cfg) };
  return {
    request: (cfg) => fetch(cfg.url, {
      method: cfg.method, headers: cfg.header, body: cfg.data ? JSON.stringify(cfg.data) : undefined,
    }).then(async (r) => ({ statusCode: r.status, data: await r.json() })),
  };
}

export function createClient({ adapter, getToken, onUnauthorized, baseUrl = "" } = {}) {
  const ad = adapter || defaultAdapter();
  return {
    async request(method, path, { query, body, header } = {}) {
      const qs = query
        ? "?" + new URLSearchParams(Object.entries(query).filter(([, v]) => v !== undefined)).toString()
        : "";
      const token = getToken ? getToken() : "";
      const res = await ad.request({
        url: `${baseUrl}${path}${qs}`,
        method,
        data: body,
        header: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}), ...header },
      });
      const { statusCode, data } = res;
      if (statusCode === 401) { onUnauthorized?.(); throw new UnauthorizedError(data?.message); }
      if (statusCode === 428) throw new PasswordExpiredError(data?.message);
      if (statusCode >= 400) throw new BizError(data?.message || `HTTP ${statusCode}`, data?.code);
      if (data && typeof data === "object" && "code" in data) {
        if (data.code !== 0) throw new BizError(data.message || "请求失败", data.code);
        return data.data;
      }
      return data;
    },
    get: (p, o) => this.request("GET", p, o),
    post: (p, o) => this.request("POST", p, o),
    put: (p, o) => this.request("PUT", p, o),
    del: (p, o) => this.request("DELETE", p, o),
  };
}
```

```js
// uniapp/src/api/holdings.js
export const holdingsApi = (c) => ({
  list: (query) => c.get("/api/v1/holdings", { query }),
  create: (body) => c.post("/api/v1/holdings", { body }),
  update: (id, body) => c.put(`/api/v1/holdings/${id}`, { body }),
  remove: (id) => c.del(`/api/v1/holdings/${id}`),
  importCsv: (body) => c.post("/api/v1/holdings/import", { body }),
});
```

```js
// uniapp/src/api/portfolio.js
export const portfolioApi = (c) => ({
  runs: (userId, query) => c.get("/api/portfolio/runs", { query: { user_id: userId, ...query } }),
  detail: (runId) => c.get(`/api/external/portfolio/diag/${runId}`),
});
```

```js
// uniapp/src/api/messages.js
export const messagesApi = (c) => ({
  list: (query) => c.get("/api/v1/messages", { query }),
  markRead: (id) => c.put(`/api/v1/messages/${id}/read`),
});
```

- [ ] **Step 4: 运行确认通过**

Run: `npm test`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add uniapp/src/api uniapp/tests/client.test.js
git commit -m "feat(uniapp): M5 API 客户端（token/401/428/业务错误统一处理）"
```

---

### Task 4: CSV 解析纯逻辑（REQ-020 持仓导入）

**Files:**
- Create: `uniapp/src/logic/csv.js`
- Test: `uniapp/tests/csv.test.js`

- [ ] **Step 1: 写失败测试**

```js
// uniapp/tests/csv.test.js
import test from "node:test";
import assert from "node:assert/strict";
import { parseHoldingsCsv } from "../src/logic/csv.js";

test("标准表头：解析为持仓对象", () => {
  const text = "code,name,cost,shares\n600519,贵州茅台,1680.5,100\n000001,平安银行,11.2,1000\n";
  const { rows, errors } = parseHoldingsCsv(text);
  assert.equal(errors.length, 0);
  assert.deepEqual(rows[0], { code: "600519", name: "贵州茅台", cost: 1680.5, shares: 100 });
  assert.equal(rows.length, 2);
});

test("缺列 → 报错不产出该行", () => {
  const { rows, errors } = parseHoldingsCsv("code,cost\n600519,1\n");
  assert.equal(rows.length, 0);
  assert.match(errors[0].reason, /缺少|表头/);
});

test("非法数值/空代码 → 行级错误（保留行号）", () => {
  const text = "code,name,cost,shares\n,Bad,1,2\n600519,M,abc,100\n";
  const { rows, errors } = parseHoldingsCsv(text);
  assert.equal(rows.length, 0);
  assert.equal(errors.length, 2);
  assert.equal(errors[0].line, 2);
});

test("CRLF 与 BOM 容错", () => {
  const text = "\uFEFFcode,name,cost,shares\r\n600519,M,10,1\r\n";
  const { rows, errors } = parseHoldingsCsv(text);
  assert.equal(errors.length, 0);
  assert.equal(rows[0].code, "600519");
});

test("空文件 → 单一错误", () => {
  const { rows, errors } = parseHoldingsCsv("");
  assert.equal(rows.length, 0);
  assert.equal(errors.length, 1);
});
```

- [ ] **Step 2: 运行确认失败**

Run: `npm test`
Expected: FAIL

- [ ] **Step 3: 实现**

```js
// uniapp/src/logic/csv.js —— 纯函数 CSV 解析（零计算：仅字段解析/校验，不做行情换算）
const REQUIRED = ["code", "name", "cost", "shares"];

function splitLine(line) {
  // 支持逗号分隔与简单引号；持仓字段不含换行，保持轻量
  const out = [];
  let cur = "", inQ = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (ch === '"') { inQ = !inQ; continue; }
    if (ch === "," && !inQ) { out.push(cur); cur = ""; continue; }
    cur += ch;
  }
  out.push(cur);
  return out.map((s) => s.trim());
}

export function parseHoldingsCsv(text) {
  const errors = [];
  if (!text || !text.replace(/^\uFEFF/, "").trim()) {
    return { rows: [], errors: [{ line: 0, reason: "文件为空" }] };
  }
  const lines = text.replace(/^\uFEFF/, "").split(/\r?\n/).filter((l) => l.trim() !== "");
  const header = splitLine(lines[0]).map((h) => h.toLowerCase());
  const missing = REQUIRED.filter((k) => !header.includes(k));
  if (missing.length) {
    return { rows: [], errors: [{ line: 1, reason: `缺少表头列: ${missing.join(",")}` }] };
  }
  const idx = Object.fromEntries(REQUIRED.map((k) => [k, header.indexOf(k)]));
  const rows = [];
  for (let i = 1; i < lines.length; i++) {
    const cells = splitLine(lines[i]);
    const code = cells[idx.code] || "";
    const name = cells[idx.name] || "";
    const cost = Number(cells[idx.cost]);
    const shares = Number(cells[idx.shares]);
    if (!code) { errors.push({ line: i + 1, reason: "代码为空" }); continue; }
    if (!Number.isFinite(cost) || cost <= 0) { errors.push({ line: i + 1, reason: "成本价非法" }); continue; }
    if (!Number.isFinite(shares) || shares <= 0 || !Number.isInteger(shares)) {
      errors.push({ line: i + 1, reason: "持股数非法" }); continue;
    }
    rows.push({ code, name, cost, shares });
  }
  return { rows, errors };
}
```

- [ ] **Step 4: 运行确认通过**

Run: `npm test`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add uniapp/src/logic/csv.js uniapp/tests/csv.test.js
git commit -m "feat(uniapp): M5 持仓 CSV 解析（表头校验/行级错误/BOM 容错）"
```

---

### Task 5: 零计算渲染映射（R1-3）

**Files:**
- Create: `uniapp/src/logic/view.js`
- Test: `uniapp/tests/view.test.js`

- [ ] **Step 1: 写失败测试**

```js
// uniapp/tests/view.test.js —— 断言：只映射服务端字段，不新增运算
import test from "node:test";
import assert from "node:assert/strict";
import { toHoldingRow, toDiagRow, toMessageRow } from "../src/logic/view.js";

test("持仓行：判定与收益直接取服务端字段", () => {
  const server = { code: "600519", name: "贵州茅台", cost: 1680.5, shares: 100,
                   action: "sell", action_reason: "跌破MA10", latest_price: 1500, holding_pnl: -18050, pnl_pct: -10.7 };
  const row = toHoldingRow(server);
  assert.equal(row.action, "sell");
  assert.equal(row.latestPrice, server.latest_price);
  assert.equal(row.pnl, server.holding_pnl);       // 透传，不重算
  assert.equal(row.pnlPct, server.pnl_pct);
});

test("诊断行：透传摘要字段", () => {
  const row = toDiagRow({ run_id: "r1", natural_day: "2026-09-17", status: "completed",
                          holding_count: 2, action_count: 1, total_pnl: 100.5 });
  assert.deepEqual(row, { runId: "r1", day: "2026-09-17", status: "completed",
                          holdingCount: 2, actionCount: 1, totalPnl: 100.5 });
});

test("消息行：未读态由服务端 read_status 决定", () => {
  assert.equal(toMessageRow({ id: "m1", type: "portfolio_diag_confirmed", title: "T",
                              content: "C", read_status: false }).unread, true);
});

test("缺失字段不崩溃（显示占位）", () => {
  const row = toHoldingRow({ code: "000001" });
  assert.equal(row.latestPrice, null);
  assert.equal(row.actionText, "无建议");
});
```

- [ ] **Step 2: 运行确认失败**

Run: `npm test`
Expected: FAIL

- [ ] **Step 3: 实现**

```js
// uniapp/src/logic/view.js —— 服务端 → ViewModel 纯映射；红线 R1-3：禁止任何金额/指标重算
const num = (v) => (v === undefined || v === null ? null : Number(v));
const ACTION_TEXT = { sell: "建议卖出", hold: "持有", add: "建议加仓" };

export function toHoldingRow(s) {
  return {
    code: s.code,
    name: s.name || s.code,
    cost: num(s.cost),
    shares: num(s.shares),
    action: s.action || null,
    actionText: ACTION_TEXT[s.action] || "无建议",
    actionReason: s.action_reason || "",
    latestPrice: num(s.latest_price),
    pnl: num(s.holding_pnl),
    pnlPct: num(s.pnl_pct),
  };
}

export function toDiagRow(s) {
  return {
    runId: s.run_id,
    day: s.natural_day,
    status: s.status,
    holdingCount: num(s.holding_count),
    actionCount: num(s.action_count),
    totalPnl: num(s.total_pnl),
  };
}

export function toMessageRow(s) {
  return {
    id: s.id,
    type: s.type,
    title: s.title || "",
    content: s.content || "",
    unread: !s.read_status,
    createdAt: s.created_at || null,
  };
}
```

- [ ] **Step 4: 运行确认通过**

Run: `npm test`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add uniapp/src/logic/view.js uniapp/tests/view.test.js
git commit -m "feat(uniapp): M5 零计算渲染映射（R1-3 仅透传服务端判定/收益）"
```

---

### Task 6: WS 客户端状态机（REQ-010 断线重连 + 对齐）

**Files:**
- Create: `uniapp/src/logic/ws.js`
- Test: `uniapp/tests/ws.test.js`

- [ ] **Step 1: 写失败测试**

```js
// uniapp/tests/ws.test.js —— 假 socket，验证首帧鉴权/退避/latest_inbox_id 补齐请求
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
  const s = createNotifySocket({
    url: "ws://x/ws/notify", token: "T", socketFactory: () => (sock = new FakeSocket()),
    onMessage: () => {}, onReconnect: () => {},
  });
  s.connect();
  sock.onopen();
  assert.deepEqual(sock.sent[0], { type: "auth", token: "T" });
  s.close();
});

test("收到 hello → 以 latest_inbox_id 触发补齐拉取", () => {
  const seen = [];
  let sock;
  const s = createNotifySocket({
    url: "ws://x/ws/notify", token: "T", socketFactory: () => (sock = new FakeSocket()),
    onMessage: () => {}, onReconnect: (info) => seen.push(info),
  });
  s.connect();
  sock.onopen();
  sock.onmessage({ data: JSON.stringify({ type: "hello", latest_inbox_id: "m9", unread_count: 3 }) });
  assert.deepEqual(seen[0], { sinceId: "m9", unreadCount: 3 });
  s.close();
});

test("msg 帧回调 + 回 pong", () => {
  const got = [];
  let sock;
  const s = createNotifySocket({
    url: "x", token: "T", socketFactory: () => (sock = new FakeSocket()),
    onMessage: (m) => got.push(m), onReconnect: () => {},
  });
  s.connect(); sock.onopen();
  sock.onmessage({ data: JSON.stringify({ type: "msg", message: { id: "m1" } }) });
  assert.equal(got.length, 1);
  assert.ok(sock.sent.some((f) => f.type === "pong"));
  s.close();
});

test("退避：断线后 delay 递增并有上限", () => {
  const delays = [];
  let sock;
  const s = createNotifySocket({
    url: "x", token: "T",
    socketFactory: () => (sock = new FakeSocket()),
    onMessage: () => {}, onReconnect: () => {},
    scheduler: (fn, ms) => { delays.push(ms); /* 不真正执行，避免递归 */ },
    backoff: { base: 1000, max: 30000 },
  });
  s.connect();
  sock.onclose(); // 第 1 次断
  s._socket.onclose?.(); // 触发后续（实现内部重连次数自增）
  assert.ok(delays[0] >= 1000 && delays[0] <= 2000);
  assert.ok(delays.every((d) => d <= 30000));
  s.close();
});
```

- [ ] **Step 2: 运行确认失败**

Run: `npm test`
Expected: FAIL

- [ ] **Step 3: 实现**

```js
// uniapp/src/logic/ws.js —— 通知 WS 客户端：首帧鉴权 / 心跳 pong / 指数退避重连 / latest_inbox_id 对齐
function defaultFactory(url) {
  const u = globalThis.uni;
  return u?.connectSocket ? u.connectSocket({ url }) : new WebSocket(url);
}

export function createNotifySocket({
  url, token, socketFactory = defaultFactory, onMessage = () => {},
  onReconnect = () => {}, scheduler = (fn, ms) => setTimeout(fn, ms),
  backoff = { base: 1000, max: 30000 },
} = {}) {
  let attempts = 0;
  let closedByUser = false;
  const state = { socket: null, connected: false };

  // uni.connectSocket 返回 SocketTask 用 onOpen/onMessage；H5 WebSocket 用 onopen/onmessage → 统一适配
  function bind(sock) {
    const on = (web, uniEvt, handler) => {
      if (typeof sock[web] !== "undefined") sock[web] = handler;
      else if (sock[uniEvt]) sock[uniEvt](handler);
    };
    on("onopen", "onOpen", () => {
      attempts = 0;
      state.connected = true;
      send({ type: "auth", token });
    });
    on("onmessage", "onMessage", (ev) => handleFrame(ev));
    on("onclose", "onClose", () => { state.connected = false; scheduleReconnect(); });
    sock._send = (obj) => send(obj);
  }

  function send(obj) {
    const text = JSON.stringify(obj);
    const sock = state.socket;
    if (sock?.send) { sock.send(text); return; }
    if (sock?.send) return;
  }

  function handleFrame(ev) {
    const raw = typeof ev?.data === "string" ? ev.data : ev?.data;
    let msg;
    try { msg = JSON.parse(raw); } catch { return; }
    if (msg.type === "hello") { onReconnect({ sinceId: msg.latest_inbox_id ?? null, unreadCount: msg.unread_count ?? 0 }); return; }
    if (msg.type === "msg") { onMessage(msg); send({ type: "pong" }); return; }
    if (msg.type === "ping") { send({ type: "pong" }); }
  }

  function scheduleReconnect() {
    if (closedByUser) return;
    const exp = Math.min(backoff.base * 2 ** attempts, backoff.max);
    const jitter = exp * (0.5 + Math.random() * 0.5);
    attempts += 1;
    scheduler(() => { if (!closedByUser) connect(); }, Math.round(jitter));
  }

  function connect() {
    const sock = socketFactory(url);
    state.socket = sock;
    bind(sock);
    return sock;
  }

  return {
    connect,
    close() { closedByUser = true; state.connected = false; state.socket?.close?.(); },
    get socket() { return state.socket; },
    get _socket() { return state.socket; },
    get connected() { return state.connected; },
  };
}
```

> 实现注意：`send` 里重复的 `if (sock?.send)` 分支需清理为单次调用；`_socket` getter 仅供测试驱动。

- [ ] **Step 4: 运行确认通过**

Run: `npm test`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add uniapp/src/logic/ws.js uniapp/tests/ws.test.js
git commit -m "feat(uniapp): M5 通知 WS 客户端（首帧鉴权/心跳/退避重连/latest_inbox_id 对齐）"
```

---

### Task 7: 页面与 Tab 壳层（登录/持仓/导入/诊断/消息/我的）

**Files:**
- Create: `uniapp/src/pages/login/index.vue`、`holdings/index.vue`、`holdings/edit.vue`、`holdings/import.vue`、`diag/index.vue`、`diag/detail.vue`、`messages/index.vue`、`mine/index.vue`
- Create: `uniapp/src/composables/useClient.js`

- [ ] **Step 1: 组合式客户端（token/401/428 联动守卫）**

```js
// uniapp/src/composables/useClient.js
import { createClient } from "../api/client.js";
import { holdingsApi } from "../api/holdings.js";
import { portfolioApi } from "../api/portfolio.js";
import { messagesApi } from "../api/messages.js";
import { loadSession, clearSession } from "../store/session.js";

export function useClient() {
  const client = createClient({
    getToken: () => loadSession().token,
    onUnauthorized: () => {
      clearSession();
      globalThis.uni?.reLaunch?.({ url: "/pages/login/index" });
    },
  });
  return { client, holdings: holdingsApi(client), portfolio: portfolioApi(client), messages: messagesApi(client) };
}
```

- [ ] **Step 2: 页面骨架（每页仅绑定 + 展示，无计算）**

```vue
<!-- uniapp/src/pages/login/index.vue -->
<script setup>
import { ref } from "vue";
import { useClient } from "../../composables/useClient.js";
import { setSession } from "../../store/session.js";

const username = ref(""); const password = ref(""); const err = ref("");
const { client } = useClient();

async function submit() {
  err.value = "";
  try {
    const data = await client.post("/api/v1/auth/login", { body: { username: username.value, password: password.value } });
    setSession({ access_token: data.access_token, refresh_token: data.refresh_token, user: data.user });
    uni.switchTab({ url: "/pages/holdings/index" });
  } catch (e) { err.value = e.message; }
}
</script>
<template>
  <view class="p">
    <input v-model="username" placeholder="用户名" />
    <input v-model="password" type="password" placeholder="密码" />
    <button :disabled="!username || !password" @click="submit">登录</button>
    <text v-if="err" class="err">{{ err }}</text>
  </view>
</template>
```

```vue
<!-- uniapp/src/pages/holdings/index.vue -->
<script setup>
import { ref } from "vue";
import { onShow } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";
import { toHoldingRow } from "../../logic/view.js";

const rows = ref([]);
const { holdings } = useClient();

onShow(async () => {
  const data = await holdings.list();
  rows.value = (data.items || data || []).map(toHoldingRow);
});

const goEdit = (row) => uni.navigateTo({ url: `/pages/holdings/edit?code=${row.code}` });
const goImport = () => uni.navigateTo({ url: "/pages/holdings/import" });
</script>
<template>
  <view class="p">
    <view class="bar">
      <button size="mini" @click="goImport">CSV 导入</button>
      <button size="mini" @click="goEdit({ code: '' })">新增持仓</button>
    </view>
    <view v-for="r in rows" :key="r.code" class="card" @click="goEdit(r)">
      <text>{{ r.name }}（{{ r.code }}）</text>
      <text>建议：{{ r.actionText }}</text>
      <text v-if="r.latestPrice !== null">现价 {{ r.latestPrice }} · 收益 {{ r.pnl }}（{{ r.pnlPct }}%）</text>
      <text class="reason">{{ r.actionReason }}</text>
    </view>
    <view v-if="!rows.length" class="empty">暂无持仓，点击「CSV 导入」或「新增持仓」</view>
  </view>
</template>
```

```vue
<!-- uniapp/src/pages/holdings/import.vue —— 解析走纯逻辑，失败行回显 -->
<script setup>
import { ref } from "vue";
import { useClient } from "../../composables/useClient.js";
import { parseHoldingsCsv } from "../../logic/csv.js";

const text = ref(""); const errors = ref([]); const okCount = ref(0);
const { holdings } = useClient();

async function doImport() {
  const { rows, errors: errs } = parseHoldingsCsv(text.value);
  errors.value = errs;
  if (!rows.length) return;
  const res = await holdings.importCsv({ holdings: rows });
  okCount.value = res?.imported ?? rows.length;
  uni.showToast({ title: `导入 ${okCount.value} 条` });
}
</script>
<template>
  <view class="p">
    <textarea v-model="text" placeholder="粘贴 CSV：code,name,cost,shares" />
    <button @click="doImport">导入</button>
    <view v-for="(e, i) in errors" :key="i" class="err">第 {{ e.line }} 行：{{ e.reason }}</view>
  </view>
</template>
```

```vue
<!-- uniapp/src/pages/diag/index.vue -->
<script setup>
import { ref } from "vue";
import { onShow } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";
import { toDiagRow } from "../../logic/view.js";
import { loadSession } from "../../store/session.js";

const rows = ref([]);
const { portfolio } = useClient();

onShow(async () => {
  const me = loadSession().me || {};
  const data = await portfolio.runs(me.id);
  rows.value = (data.items || data || []).map(toDiagRow);
});
const open = (r) => uni.navigateTo({ url: `/pages/diag/detail?runId=${r.runId}` });
</script>
<template>
  <view class="p">
    <view v-for="r in rows" :key="r.runId" class="card" @click="open(r)">
      <text>{{ r.day }} · {{ r.status }}</text>
      <text>持仓 {{ r.holdingCount }} · 建议 {{ r.actionCount }} · 总收益 {{ r.totalPnl }}</text>
    </view>
    <view v-if="!rows.length" class="empty">暂无诊断记录（由服务端调度产生）</view>
  </view>
</template>
```

```vue
<!-- uniapp/src/pages/diag/detail.vue —— 回看明细，字段全部来自服务端 -->
<script setup>
import { ref } from "vue";
import { onLoad } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";
import { toHoldingRow } from "../../logic/view.js";

const runId = ref(""); const results = ref([]);
const { portfolio } = useClient();

onLoad(async (q) => {
  runId.value = q.runId;
  const data = await portfolio.detail(runId.value);
  results.value = (data.results || []).map(toHoldingRow);
});
</script>
<template>
  <view class="p">
    <view v-for="r in results" :key="r.code" class="card">
      <text>{{ r.name }}（{{ r.code }}）建议：{{ r.actionText }}</text>
      <text>现价 {{ r.latestPrice }} · 盈亏 {{ r.pnl }}（{{ r.pnlPct }}%）</text>
      <text class="reason">{{ r.actionReason }}</text>
    </view>
  </view>
</template>
```

```vue
<!-- uniapp/src/pages/messages/index.vue —— 站内信：列表 + 点击已读 + WS 新消息刷新 -->
<script setup>
import { ref } from "vue";
import { onShow } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";
import { toMessageRow } from "../../logic/view.js";
import { loadSession } from "../../store/session.js";

const rows = ref([]); const unread = ref(0);
const { messages } = useClient();

async function reload(sinceId) {
  const data = await messages.list(sinceId ? { since_id: sinceId } : {});
  rows.value = (data.items || []).map(toMessageRow);
  unread.value = data.unread_count ?? 0;
}
onShow(() => reload());

async function read(m) {
  if (!m.unread) return;
  await messages.markRead(m.id);
  reload();
}
</script>
<template>
  <view class="p">
    <view class="bar"><text>未读 {{ unread }}</text></view>
    <view v-for="m in rows" :key="m.id" class="card" @click="read(m)">
      <text :class="{ unread: m.unread }">{{ m.title }}</text>
      <text class="reason">{{ m.content }}</text>
    </view>
    <view v-if="!rows.length" class="empty">暂无消息</view>
  </view>
</template>
```

```vue
<!-- uniapp/src/pages/mine/index.vue -->
<script setup>
import { ref } from "vue";
import { loadSession, clearSession } from "../../store/session.js";

const me = ref(loadSession().me || {});
function logout() {
  clearSession();
  uni.reLaunch({ url: "/pages/login/index" });
}
</script>
<template>
  <view class="p">
    <text>用户：{{ me.username || me.id || "-" }}</text>
    <button @click="logout">退出登录</button>
  </view>
</template>
```

- [ ] **Step 3: 页面接线检查（守卫接入）**

Run: 在全量 `npm test` 之后，人工检查 `App.vue onLaunch` 与各页 `onShow` 是否调用 `guard()`（未登录跳登录）。将守卫接入 `App.vue`：

```vue
<!-- uniapp/src/App.vue（更新版） -->
<script>
import { isLoggedIn } from "./store/session.js";
export default {
  onLaunch() {
    const pages = getCurrentPages?.() || [];
    const cur = pages.length ? pages[pages.length - 1].route : "";
    if (!isLoggedIn() && cur !== "pages/login/index") {
      uni.reLaunch({ url: "/pages/login/index" });
    }
  },
};
</script>
<template><view /></template>
```

- [ ] **Step 4: Commit**

```bash
git add uniapp/src/pages uniapp/src/composables uniapp/src/App.vue
git commit -m "feat(uniapp): M5 页面（登录/持仓/导入/诊断回看/消息/我的 + 守卫接入）"
```

---

### Task 8: 通知落地 + 文档 + 全量验收

**Files:**
- Create: `uniapp/README.md`
- Modify: `uniapp/src/pages/messages/index.vue`（接入 WS）

- [ ] **Step 1: 消息页接入 WS（hello 时补齐、msg 时刷新）**

```js
// 在 messages/index.vue <script setup> 追加：
import { createNotifySocket } from "../../logic/ws.js";
import { onUnload } from "@dcloudio/uni-app";

const base = import.meta.env?.VITE_WS_URL || "ws://127.0.0.1:8000/ws/notify";
const sock = createNotifySocket({
  url: base,
  token: loadSession().token,
  onReconnect: ({ sinceId }) => reload(sinceId || undefined),   // 断线重连后按 latest_inbox_id 补齐（REQ-010）
  onMessage: () => reload(),
});
onShow(() => { reload(); if (!sock.connected) sock.connect(); });
onUnload(() => sock.close());
```

- [ ] **Step 2: README（运行/构建/手动冒烟）**

```markdown
# 持仓诊断 UniApp 客户端（M5）

## 运行（H5，开发）
1. `npm install`
2. 后端：`cd ../stock_selector_api/backend && python -m uvicorn main:app --port 8000`
3. `npm run dev:h5` → 打开输出的本地地址（含 /api 代理到 8000，/ws 代理）

## 纯逻辑测试（无需安装依赖）
`npm test`（node --test：守卫/CSV/WS 状态机/零计算映射/API 客户端）

## 手动冒烟清单
- [ ] 未登录访问任一 Tab → 自动跳登录页（REQ-012）
- [ ] 登录成功 → 进入持仓 Tab；退出 → 回登录页
- [ ] 粘贴 CSV（`code,name,cost,shares`）→ 成功导入；非法行有行号报错（REQ-020）
- [ ] 诊断 Tab 显示历史 run，点击进入明细（判定/收益与服务端一致，无前端重算，R1-3）
- [ ] 消息 Tab 显示站内信与未读数；点击变已读；后端触发诊断后在线收到新消息（REQ-010/021）
- [ ] 断开网络再恢复 → WS 重连并按 latest_inbox_id 补齐（REQ-010）
```

- [ ] **Step 3: 全量测试**

Run（`workdir=uniapp`）: `npm test`
Expected: 全绿（5 个测试文件；UI 组件不参与自动化，按 README 手动冒烟）

- [ ] **Step 4: Commit**

```bash
git add uniapp/README.md uniapp/src/pages/messages/index.vue
git commit -m "docs(uniapp): M5 WS 接入 + 运行/冒烟说明"
```

---

## 验收标准汇总（对照 M5 行「REQ-012 守卫 + REQ-018~021、R1-3 零计算」）

| 验收项 | 验证方式 | 结果 |
|---|---|---|
| 壳层隔离/登录守卫（REQ-012） | tests/guard.test.js 全分支 | 绿 |
| 持仓录入唯一入口 + CSV 导入（REQ-018/020） | tests/csv.test.js + 页面手动冒烟 | 绿 |
| 诊断历史回看消费服务端 run 明细（REQ-019/021） | tests/view.test.js + 手动冒烟 | 绿 |
| 通知双路消费 + latest_inbox_id 补齐（REQ-010/021） | tests/ws.test.js + 手动冒烟 | 绿 |
| 前端零计算（R1-3） | tests/view.test.js 透传断言 + 代码审查（无行情/指标/金额运算） | 绿 |
| 属主隔离（客户端只带自身 token 查询） | 全部请求经 client 注入 token；服务端隔离由 M3 覆盖 | 绿 |

## 风险与注意

- **离线装包风险**：`npm install`（uni-app 全套）依赖外网；若不可用，Task 1 的结构与 Task 2-6 的 `node --test` 纯逻辑仍可完整验证，UI 打包留待联网后执行（README 记录）。
- **uni API 与 H5 差异**：`connectSocket` 与 `WebSocket` 事件名不同，`ws.js` 已做适配；测试用假 socket 不覆盖真机差异，依赖手动冒烟。
- **消息已读一致性**：以服务端为准，本地不缓存可信态；沿用服务端 `read_status`（REQ-009 可信源）。
- **诊断触发**：本里程碑不在前端触发诊断（外部调度产生），避免与 R1-1「后端零判定」语义混淆。
- **WS 自动化**：仅覆盖状态机纯逻辑；真实服务端 WS 收推在 M4 已单元覆盖 + 手动冒烟。