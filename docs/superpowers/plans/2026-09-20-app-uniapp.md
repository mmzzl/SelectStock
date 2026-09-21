# 智能选股 App — uniapp 前端实施计划（M6–M11）
> 子系统：uniapp 前端（H5 + App 打包），消费 backend 计划（`2026-09-20-app-backend.md`，B1–B19）产出的全部契约
> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付 `D:\select_stock\uniapp` 客户端：5 Tab 壳层（持仓/选股/诊断/消息/我的）+ 交割单双步导入 + 交易记录 + 利润分析 + 选股（手动/历史）+ 消息中心 + WS 实时 + 自动更新（H5 检查 + App uni-upgrade-app 逻辑），全部满足 **R1-3 前端零计算**：只渲染服务端判定/收益/选股结果，纯逻辑放 `src/logic/*` 用 `node --test` 验证。

**Architecture:** UniApp（Vue3 + Vite）。`src/api/client.js` 统一封装（token 注入、401 登出、428 改密跳转、ok 解包、multipart 上传 adapter、raw 文本上传）；纯逻辑（路由守卫、WS 状态机、版本比对、零计算渲染映射）放 `src/logic/*.js`，不依赖 uni 运行时，Node 内建 `node --test` 断言（零额外依赖、离线可跑）；UI 层只绑定与展示。H5 dev 用 vite 代理 `/api`、`/ws` 到 8000；App 构建用 `VITE_API_BASE`/`VITE_WS_URL` 覆盖。

**Tech Stack:** Vue3 + UniApp(vite) · Node ≥18 `node --test` · 既有后端 `/api/v1/*`、`/api/portfolio/*`、`/api/external/portfolio/*`、`/ws/notify`、`/api/app/*`

**Spec:** `docs/superpowers/specs/2026-09-20-select-app-uniapp-design.md`

## Global Constraints
- R1-3：**前端零计算**。`src/logic/view.js` 与全部页面不得出现金额/收益率/判定算术；所有盈亏、profit、选股结果、诊断 action 一律透传。唯一允许的比较逻辑：版本号比对（`version_code`，元信息判定，不属"计算"）。
- 测试一律 `node --test`（`uniapp/tests/*.test.js`），零第三方依赖；若 `npm install` 离线，U1 脚手架结构与 U2–U4 纯逻辑仍可完整验证，UI 打包留待联网（README 记录）。
- 契约不可变：消费 backend 计划 B1–B18 的接口与字段（下表）；实现中若发现契约不符，uni 端优先适配，必要时回写 backend 计划并在 spec 同步。
- 属主隔离：全部 HTTP 经 `client` 注入 token；WS 首帧 `{"type":"auth","token"}`；不做任何跨用户查询。
- Tab 为 5 个（持仓/选股/诊断/消息/我的）。切断旧 4 Tab 规划（`2026-09-17-portfolio-m5-uniapp.md`）仅为参考模式，本计划独立建成。
- PC/手机自适应：H5 宽屏容器用条件样式/断点落地，不阻塞功能；移动优先，PC 只保证可读可用。

**后端契约（消费方，字段以 backend 计划 B-任务为准）**

| 能力 | 方法 路径 | 备注 |
|---|---|---|
| 登录 | `POST /api/v1/auth/login` | → `{access_token, refresh_token, user}` |
| 当前用户 | `GET /api/v1/auth/me` | 守卫判定登录态 |
| 持仓 | `GET/POST /api/v1/holdings`、`PUT/DELETE /api/v1/holdings/{id}` | 列表项附诊断快照：`latest_price/holding_pnl/pnl_pct/action/action_reason/diag_run_id`（B8） |
| 利润 | `GET /api/v1/holdings/pnl?natural_day=` | 透传插件 `{per_code, realized_summary, unrealized_summary}`（B8） |
| 交割单预览 | `POST /api/v1/trades/import/preview` | multipart `file` 或 raw 文本体；返回 `{total, valid, errors:[{line,reason}], items}`（B6） |
| 交割单提交 | `POST /api/v1/trades/import/commit` | body `{rows:[...TradeRow], batch_note}` → `{imported, holdings_changed, errors}`（B6） |
| 交易记录 | `GET /api/v1/trades?page&size&code&direction&trade_date_from&trade_date_to`（B6） |
| 手动录入 | `POST /api/v1/trades`（B6） |
| 删除 | `DELETE /api/v1/trades/{id}`（仅 manual 源，B6） |
| 选股 | `GET /api/v1/selection/strategies`；`POST /api/v1/selection/run`；`GET /api/v1/selection/runs?page&size`；`GET /api/v1/selection/runs/{job_id}`（B13/B14） | job 内含 `strategies`(JSON 字符串)、`status`、`results[]` |
| 诊断 | `GET /api/portfolio/runs?user_id=`、`GET /api/external/portfolio/diag/{run_id}`（既有） |
| 站内信 | `GET /api/v1/messages?page&size&unread_only&since_id`；`PUT /api/v1/messages/{id}/read`；`PUT /api/v1/messages/read_all`（B10） | 返回 `{items,total,unread_count}`；item 有 `read_status`（true=未读） |
| 实时 | `WS /ws/notify` | 首帧 auth → `hello{latest_inbox_id,unread_count}`；`msg` 帧；`ping`→`pong`（B11） |
| 版本 | `GET /api/app/latest?platform=h5|android|ios&version_code=N` → `{has_update, latest}`；`GET /api/app/version`（B18） | `latest` 含 `version_code/version_name/url/md5/size_bytes/release_notes/forced/package_type` |

---

## 文件映射（uniapp）

| 文件 | 动作 | 职责 |
|---|---|---|
| `uniapp/package.json`、`vite.config.js` | 新增(U1) | 工程与脚本；vite 代理 `/api`、`/ws` → 8000 |
| `uniapp/src/manifest.json` | 新增(U1) | 版本号/App 配置 |
| `uniapp/src/pages.json` | 新增(U1) | 全部页面 + 5 Tab |
| `uniapp/src/main.js`、`src/App.vue` | 新增(U1)；修改(U11) | 入口；守卫 + 版本检查启动钩子 |
| `uniapp/tests/setup-smoke.test.js` | 新增(U1) | 结构冒烟（5 Tab） |
| `uniapp/src/logic/guard.js` | 新增(U2) | 纯函数：路由/登录隔离（含 428） |
| `uniapp/src/store/session.js` | 新增(U2) | 登录态/token 存储（uni.storage 适配） |
| `uniapp/tests/guard.test.js` | 新增(U2) | 守卫全分支 |
| `uniapp/src/api/client.js` | 新增(U3) | token/401/428/ok 解包 + `upload` + `rawBody` |
| `uniapp/src/api/holdings.js`、`trades.js`、`selection.js`、`messages.js`、`upgrade.js` | 新增(U3) | 领域 API |
| `uniapp/src/composables/useClient.js` | 新增(U3) | 组合式客户端 + 401 登出联动 |
| `uniapp/tests/client.test.js` | 新增(U3) | client 行为 |
| `uniapp/src/logic/view.js` | 新增(U4) | 零计算渲染映射（R1-3） |
| `uniapp/tests/view.test.js` | 新增(U4) | 透传断言，禁止重算 |
| `uniapp/src/pages/login/index.vue`、`mine/index.vue` | 新增(U3)；修改(U11) | 登录/我的（退出） |
| `uniapp/src/pages/holdings/import.vue` | 新增(U5) | 交割单双步导入（上传/粘贴→预览→提交） |
| `uniapp/src/pages/holdings/index.vue`、`edit.vue` | 新增(U6) | 持仓列表（附快照）/编辑 |
| `uniapp/src/pages/profit/index.vue` | 新增(U6) | 利润分析（透传 /holdings/pnl） |
| `uniapp/src/pages/trades/index.vue` | 新增(U7) | 交易记录（筛选/删除/手动录入） |
| `uniapp/src/pages/selection/index.vue`、`history.vue` | 新增(U8) | 选股触发+轮询+结果 / 历史回看 |
| `uniapp/src/pages/diag/index.vue`、`detail.vue` | 新增(U9) | 诊断历史/明细回看 |
| `uniapp/src/logic/ws.js` | 新增(U10) | WS 状态机（首帧鉴权/退避/latest_inbox_id） |
| `uniapp/tests/ws.test.js` | 新增(U10) | WS 状态机 |
| `uniapp/src/pages/messages/index.vue` | 新增(U10) | 消息中心 + WS 实时 |
| `uniapp/src/logic/version.js` | 新增(U11) | 版本比对纯函数 |
| `uniapp/tests/version.test.js` | 新增(U11) | 版本/强制升级判定 |
| `scripts/samples/sample_deal.csv` | 新增(U12) | 联调样例交割单 |
| `uniapp/README.md` | 新增(U13) | 运行/构建/冒烟清单 |
| `docs/deploy/2026-09-20-app.md` | 新增(U13) | 部署文档 |

---

### Task U1: 工程脚手架 + 5 Tab + 测试运行器（M6）

**Files:**
- Create: `uniapp/package.json`、`uniapp/vite.config.js`、`uniapp/src/manifest.json`、`uniapp/src/pages.json`、`uniapp/src/main.js`、`uniapp/src/App.vue`
- Create: `uniapp/tests/setup-smoke.test.js`

- [ ] **Step 1: 探测模板可用性（离线优先）**

Run（`workdir=uniapp` 或仓库根）: `npm ping` / `npx --no-install degit --help`
预期：离线 → 采用最小手工结构（下述），不依赖网络模板；在线 → `npx degit dcloudio/uni-preset-vue#vite .` 作基线再覆盖。

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
    "build:app": "uni build -p app"
  },
  "devDependencies": {
    "@dcloudio/uni-app": "^3.0.0-40010201",
    "@dcloudio/uni-h5": "^3.0.0-40010201",
    "@dcloudio/uni-app-plus": "^3.0.0-40010201",
    "@dcloudio/vite-plugin-uni": "^3.0.0-40010201",
    "vite": "^5.2.0",
    "vue": "^3.4.21"
  }
}
```

```js
// uniapp/vite.config.js —— dev 代理后端（避免跨域）；App 构建用 VITE_API_BASE/VITE_WS_URL
import { defineConfig, loadEnv } from "vite";
import uni from "@dcloudio/vite-plugin-uni";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  return {
    plugins: [uni()],
    define: {
      __VITE_API_BASE__: JSON.stringify(env.VITE_API_BASE || ""),
      __VITE_WS_URL__: JSON.stringify(env.VITE_WS_URL || "ws://127.0.0.1:8000/ws/notify"),
      __H5_VER_CODE__: JSON.stringify(env.H5_VER_CODE || "100"),   // 与 manifest versionCode 同步
      __APP_VER_CODE__: JSON.stringify(env.APP_VER_CODE || "100"), // 同上
    },
    server: {
      port: 5173,
      proxy: {
        "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
        "/ws": { target: "ws://127.0.0.1:8000", ws: true },
      },
    },
  };
});
```

```json
// uniapp/src/manifest.json（最小）
{"name":"select-stock","appid":"","description":"智能选股客户端","versionName":"0.1.0","versionCode":"100","h5":{"title":"智能选股"},"app-plus":{"distribute":{"android":{"packagename":{"name":"com.select.stock"}}}}}
```

```json
// uniapp/src/pages.json —— 登录页 + 5 Tab 壳层 + 子页
{
  "pages": [
    { "path": "pages/login/index", "style": { "navigationBarTitleText": "登录" } },
    { "path": "pages/holdings/index", "style": { "navigationBarTitleText": "持仓" } },
    { "path": "pages/holdings/edit", "style": { "navigationBarTitleText": "编辑持仓" } },
    { "path": "pages/holdings/import", "style": { "navigationBarTitleText": "交割单导入" } },
    { "path": "pages/trades/index", "style": { "navigationBarTitleText": "交易记录" } },
    { "path": "pages/profit/index", "style": { "navigationBarTitleText": "利润分析" } },
    { "path": "pages/selection/index", "style": { "navigationBarTitleText": "选股" } },
    { "path": "pages/selection/history", "style": { "navigationBarTitleText": "选股历史" } },
    { "path": "pages/diag/index", "style": { "navigationBarTitleText": "诊断历史" } },
    { "path": "pages/diag/detail", "style": { "navigationBarTitleText": "诊断明细" } },
    { "path": "pages/messages/index", "style": { "navigationBarTitleText": "消息" } },
    { "path": "pages/mine/index", "style": { "navigationBarTitleText": "我的" } },
    { "path": "pages/mine/password", "style": { "navigationBarTitleText": "修改密码" } }
  ],
  "tabBar": {
    "list": [
      { "pagePath": "pages/holdings/index", "text": "持仓" },
      { "pagePath": "pages/selection/index", "text": "选股" },
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
<!-- uniapp/src/App.vue —— 守卫入口（逻辑见 logic/guard.js；版本钩子在 U11 追加） -->
<script>
import { isLoggedIn } from "./store/session.js";   // 依赖 U2，先建文件占位再接线
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

```js
// uniapp/tests/setup-smoke.test.js
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

test("pages.json 含 5 个 Tab 与全部页面", () => {
  const cfg = JSON.parse(readFileSync(new URL("../src/pages.json", import.meta.url)));
  const paths = cfg.pages.map((p) => p.path);
  for (const p of ["pages/login/index", "pages/holdings/import", "pages/trades/index",
                   "pages/profit/index", "pages/selection/index", "pages/selection/history",
                   "pages/diag/index", "pages/diag/detail", "pages/messages/index", "pages/mine/index"])
    assert.ok(paths.includes(p), `missing ${p}`);
  assert.equal(cfg.tabBar.list.length, 5);
  assert.deepEqual(cfg.tabBar.list.map((t) => t.text), ["持仓", "选股", "诊断", "消息", "我的"]);
});
```

- [ ] **Step 3: 运行测试**

Run（`workdir=uniapp`）: `npm test`
Expected: PASS（仅结构断言，不依赖 uni 运行时；`store/session.js` 缺省则临时用 `export const isLoggedIn = () => false`，U2 覆盖）

- [ ] **Step 4: Commit**

```bash
git add uniapp
git commit -m "feat(uniapp): M6 脚手架（5 Tab/pages/vite 代理/结构冒烟）"
```

---

### Task U2: 路由守卫（含 428）+ 会话存储（M6）

**Files:**
- Create: `uniapp/src/logic/guard.js`、`uniapp/src/store/session.js`
- Create: `uniapp/tests/guard.test.js`
- (U1 中 App.vue 的守卫接线在此步完善)

- [ ] **Step 1: 写失败测试**

```js
// uniapp/tests/guard.test.js
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
```

- [ ] **Step 2: 运行确认失败**

Run: `npm test` → Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

```js
// uniapp/src/logic/guard.js —— 纯函数，无 uni 依赖，便于 node --test
export const TAB_PATHS = [
  "pages/holdings/index",
  "pages/selection/index",
  "pages/diag/index",
  "pages/messages/index",
  "pages/mine/index",
];

export const PAGE_PATHS = [
  ...TAB_PATHS,
  "pages/login/index",
  "pages/holdings/edit",
  "pages/holdings/import",
  "pages/trades/index",
  "pages/profit/index",
  "pages/selection/history",
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
// uniapp/src/store/session.js
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

Run: `npm test` → Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add uniapp/src/logic/guard.js uniapp/src/store/session.js uniapp/tests/guard.test.js
git commit -m "feat(uniapp): M6 路由守卫（登录/428改密/未知路径，5 Tab 隔离）"
```

---

### Task U3: API 客户端（token/401/428 + 上传/raw 文本）+ 领域 API + 登录/我的页面（M6）

**Files:**
- Create: `uniapp/src/api/client.js`、`uniapp/src/api/holdings.js`、`uniapp/src/api/trades.js`、`uniapp/src/api/selection.js`、`uniapp/src/api/messages.js`、`uniapp/src/api/upgrade.js`、`uniapp/src/composables/useClient.js`
- Create: `uniapp/src/pages/login/index.vue`、`uniapp/src/pages/mine/index.vue`、`uniapp/src/pages/mine/password.vue`
- Create: `uniapp/tests/client.test.js`

- [ ] **Step 1: 写失败测试**

```js
// uniapp/tests/client.test.js —— 注入假适配器，验证 token/401/428/解包/raw/上传
import test from "node:test";
import assert from "node:assert/strict";
import { createClient, UnauthorizedError, PasswordExpiredError, BizError } from "../src/api/client.js";

function fakeAdapter() {
  const calls = [];
  return {
    calls,
    request(cfg) { calls.push(cfg); return Promise.resolve(this._next); },
    upload(cfg) { calls.push(cfg); return Promise.resolve(this._next); },
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
});

test("428：抛 PasswordExpiredError（跳改密）", async () => {
  const a = fakeAdapter();
  a._next = { statusCode: 428, data: { code: 428, message: "密码已过期" } };
  const c = createClient({ adapter: a, getToken: () => "T1" });
  await assert.rejects(() => c.get("/api/v1/holdings"), (e) => e.name === "PasswordExpiredError");
});

test("业务失败 code!=0：抛 BizError 带 message", async () => {
  const a = fakeAdapter();
  a._next = { statusCode: 200, data: { code: 400, message: "持仓上限 200" } };
  const c = createClient({ adapter: a, getToken: () => "T1" });
  await assert.rejects(() => c.post("/api/v1/trades/import/commit", { body: {} }),
    (e) => e.name === "BizError" && /200/.test(e.message));
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
```

- [ ] **Step 2: 运行确认失败**

Run: `npm test` → Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 client**

```js
// uniapp/src/api/client.js
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
  if (u?.request) return {
    request: (cfg) => u.request(cfg),
    upload: (cfg) => u.uploadFile(cfg),
  };
  return {
    async request(cfg) {
      const r = await fetch(cfg.url, {
        method: cfg.method,
        headers: cfg.header,
        body: cfg.data == null ? undefined : (typeof cfg.data === "string" ? cfg.data : JSON.stringify(cfg.data)),
      });
      return { statusCode: r.status, data: await r.json().catch(() => ({})) };
    },
    upload(cfg) { throw new Error("环境无上传实现"); },
  };
}

function handle({ statusCode, data }, { onUnauthorized }) {
  if (statusCode === 401) { onUnauthorized?.(); throw new UnauthorizedError(data?.message); }
  if (statusCode === 428) throw new PasswordExpiredError(data?.message);
  if (statusCode >= 400) throw new BizError(data?.message || `HTTP ${statusCode}`, data?.code);
  if (data && typeof data === "object" && "code" in data) {
    if (data.code !== 0) throw new BizError(data.message || "请求失败", data.code);
    return data.data;
  }
  return data;
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
      return handle(res, { onUnauthorized });
    },
    async upload(path, { filePath, name = "file", formData = {}, query, header = {} } = {}) {
      const qs = query ? "?" + new URLSearchParams(Object.entries(query)).toString() : "";
      const token = getToken ? getToken() : "";
      const res = await ad.upload({
        url: `${baseUrl}${path}${qs}`,
        filePath, name, formData,
        header: { ...(token ? { Authorization: `Bearer ${token}` } : {}), ...header },
      });
      let data = res.data;
      if (typeof data === "string") { try { data = JSON.parse(data); } catch { /* 透传 */ } }
      return handle({ statusCode: res.statusCode, data }, { onUnauthorized });
    },
    get(p, o) { return this.request("GET", p, o); },
    post(p, o) { return this.request("POST", p, o); },
    put(p, o) { return this.request("PUT", p, o); },
    del(p, o) { return this.request("DELETE", p, o); },
  };
}
```

> 注：`get/post/put/del` 用对象方法简写绑定 `this`；不可用箭头函数（`this` 会丢失）。

```js
// uniapp/src/api/holdings.js
export const holdingsApi = (c) => ({
  list: (query = {}) => c.get("/api/v1/holdings", { query }),
  create: (body) => c.post("/api/v1/holdings", { body }),
  update: (id, body) => c.put(`/api/v1/holdings/${id}`, { body }),
  remove: (id) => c.del(`/api/v1/holdings/${id}`),
  pnl: (query = {}) => c.get("/api/v1/holdings/pnl", { query }),
});
```

```js
// uniapp/src/api/trades.js
export const tradesApi = (c) => ({
  previewFile: (filePath, name = "file") =>
    c.upload("/api/v1/trades/import/preview", { filePath, name }),
  previewText: (text) =>
    c.request("POST", "/api/v1/trades/import/preview",
      { body: text, header: { "Content-Type": "text/plain" } }),
  commit: (rows, batch_note = "") =>
    c.post("/api/v1/trades/import/commit", { body: { rows, batch_note } }),
  list: (query = {}) => c.get("/api/v1/trades", { query }),
  create: (row) => c.post("/api/v1/trades", { body: row }),
  remove: (id) => c.del(`/api/v1/trades/${id}`),
});
```

```js
// uniapp/src/api/selection.js
export const selectionApi = (c) => ({
  strategies: () => c.get("/api/v1/selection/strategies"),
  run: (strategies) => c.post("/api/v1/selection/run", { body: { strategies } }),
  runs: (query = {}) => c.get("/api/v1/selection/runs", { query }),
  runDetail: (jobId) => c.get(`/api/v1/selection/runs/${jobId}`),
});
```

```js
// uniapp/src/api/messages.js
export const messagesApi = (c) => ({
  list: (query = {}) => c.get("/api/v1/messages", { query }),
  markRead: (id) => c.put(`/api/v1/messages/${id}/read`),
  markAllRead: () => c.put("/api/v1/messages/read_all"),
});
```

```js
// uniapp/src/api/upgrade.js
export const upgradeApi = (c) => ({
  latest: (query = {}) => c.get("/api/app/latest", { query }),
  version: () => c.get("/api/app/version"),
});
```

```js
// uniapp/src/composables/useClient.js
import { createClient } from "../api/client.js";
import { holdingsApi } from "../api/holdings.js";
import { tradesApi } from "../api/trades.js";
import { selectionApi } from "../api/selection.js";
import { messagesApi } from "../api/messages.js";
import { upgradeApi } from "../api/upgrade.js";
import { loadSession, clearSession, setSession } from "../store/session.js";

const BASE = globalThis.__VITE_API_BASE__ || "";

let _cached = null;
export function useClient() {
  if (_cached) return _cached;
  const client = createClient({
    baseUrl: BASE,
    getToken: () => loadSession().token,
    onUnauthorized: () => {
      clearSession();
      globalThis.uni?.reLaunch?.({ url: "/pages/login/index" });
    },
  });
  _cached = {
    client, setSession,
    holdings: holdingsApi(client),
    trades: tradesApi(client),
    selection: selectionApi(client),
    messages: messagesApi(client),
    upgrade: upgradeApi(client),
  };
  return _cached;
}
```

- [ ] **Step 4: 登录 / 我的 / 修改密码页面（壳层）**

```vue
<!-- uniapp/src/pages/login/index.vue -->
<script setup>
import { ref } from "vue";
import { useClient } from "../../composables/useClient.js";

const username = ref(""); const password = ref(""); const err = ref("");
const { client, setSession } = useClient();

async function submit() {
  err.value = "";
  try {
    const data = await client.post("/api/v1/auth/login", { body: { username: username.value, password: password.value } });
    setSession(data);
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
<!-- uniapp/src/pages/mine/index.vue —— 退出 + 更新入口占位（U11 增强版本检查） -->
<script setup>
import { ref } from "vue";
import { loadSession, clearSession } from "../../store/session.js";
const me = ref(loadSession().me || {});
function logout() { clearSession(); uni.reLaunch({ url: "/pages/login/index" }); }
</script>
<template>
  <view class="p">
    <text>用户：{{ me.username || me.id || "-" }}</text>
    <button @click="logout">退出登录</button>
  </view>
</template>
```

> `pages/mine/password.vue`：占位页（提示 + 重新登录按钮）。改密接口语义在后端既有能力范围内于 U12 联调时核对；若不存在则不接提交按钮，URN01 风险记录。

- [ ] **Step 5: 运行确认通过**

Run: `npm test` → Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add uniapp/src/api uniapp/src/composables uniapp/src/pages/login uniapp/src/pages/mine uniapp/tests/client.test.js
git commit -m "feat(uniapp): M6 API 客户端（token/401/428/上传/raw）+ 登录/我的壳层"
```

---

### Task U4: 零计算渲染映射（R1-3）

**Files:**
- Create: `uniapp/src/logic/view.js`
- Create: `uniapp/tests/view.test.js`

- [ ] **Step 1: 写失败测试**

```js
// uniapp/tests/view.test.js —— 只映射服务端字段；断言禁止新增金额/收益率运算
import test from "node:test";
import assert from "node:assert/strict";
import { toHoldingRow, toTradeRow, toPnlView, toSelectionJobRow, toSelectionResultRow,
         toMessageRow, toDiagRow, toPreviewRow, toUpgradeInfo } from "../src/logic/view.js";

test("持仓行：快照字段透传，不重算", () => {
  const s = { code: "600519", name: "贵州茅台", cost: 1680.5, shares: 100,
              action: "sell", action_reason: "跌破MA10", latest_price: 1500,
              holding_pnl: -18050, pnl_pct: -10.7, diag_run_id: "r9" };
  const row = toHoldingRow(s);
  assert.equal(row.latestPrice, 1500);
  assert.equal(row.pnl, -18050);          // 透传
  assert.equal(row.pnlPct, -10.7);
  assert.equal(row.action, "sell");
  assert.equal(row.diagRunId, "r9");
});

test("利润视图：per_code 与汇总原样透传（禁止求和/取百分比）", () => {
  const res = {
    per_code: [{ code: "600519", name: "茅台", position: 60, avg_cost: 1600,
                 latest_price: 1800, realized_pnl: 7923, unrealized_pnl: 12000,
                 total_pnl: 19923, fees: 77 }],
    realized_summary: { total_realized: 7923, total_fees: 77 },
    unrealized_summary: { total_unrealized: 12000, total_value: 108000, holding_count: 1 },
  };
  const v = toPnlView(res);
  assert.equal(v.rows[0].realizedPnl, 7923);
  assert.equal(v.realized.totalRealized, 7923);
  assert.equal(v.unrealized.holdingCount, 1);
});

test("利润缺失字段 → null 占位，不崩溃", () => {
  const v = toPnlView({ per_code: [] });
  assert.deepEqual(v.rows, []);
});

test("交易记录行：方向文本映射，金额透传", () => {
  const s = { id: "t1", trade_date: "2026-09-01", stock_code: "600519", stock_name: "茅台",
              direction: "sell", price: 1800, quantity: 40, settled_amount: 72000, source: "import" };
  assert.deepEqual(toTradeRow(s).directionText, "卖出");
  assert.equal(toTradeRow(s).settledAmount, 72000);
});

test("选股 job：strategies JSON 字符串安全解析", () => {
  const j = toSelectionJobRow({ job_id: "j1", strategies: '["trend_rebound"]', status: "done", result_count: 2 });
  assert.deepEqual(j.strategies, ["trend_rebound"]);
  assert.ok(Array.isArray(toSelectionJobRow({ strategies: "bad-json" }).strategies));
});

test("消息行：未读态由 read_status 决定", () => {
  assert.equal(toMessageRow({ id: "m1", read_status: false }).unread, true);
  assert.equal(toMessageRow({ id: "m2", read_status: true }).unread, false);
});

test("版本信息：透传并标注更新判定字段", () => {
  const info = toUpgradeInfo({ has_update: true, latest: { version_code: 102, version_name: "1.0.2",
    url: "/u/a.wgt", md5: "m", release_notes: "修复", forced: true, package_type: "wgt" } });
  assert.equal(info.hasUpdate, true);
  assert.equal(info.latest.forced, true);
});
```

- [ ] **Step 2: 运行确认失败**

Run: `npm test` → Expected: FAIL

- [ ] **Step 3: 实现**

```js
// uniapp/src/logic/view.js —— 服务端 → ViewModel 纯映射；R1-3：禁止任何金额/指标重算
const num = (v) => (v === undefined || v === null ? null : Number(v));
const ACTION_TEXT = { sell: "建议卖出", hold: "持有", add: "建议加仓" };
const DIR_TEXT = { buy: "买入", sell: "卖出" };

function parseStrList(raw) {
  if (Array.isArray(raw)) return raw.map(String);
  if (typeof raw !== "string") return [];
  try { const v = JSON.parse(raw); return Array.isArray(v) ? v : []; } catch { return []; }
}

export function toHoldingRow(s) {
  return {
    code: s.code,
    name: s.name || s.code,
    cost: num(s.cost), shares: num(s.shares),
    action: s.action || null,
    actionText: ACTION_TEXT[s.action] || "无建议",
    actionReason: s.action_reason || "",
    latestPrice: num(s.latest_price),
    pnl: num(s.holding_pnl), pnlPct: num(s.pnl_pct),
    diagRunId: s.diag_run_id || null,
  };
}

export function toPnlRow(s) {
  return {
    code: s.code, name: s.name || s.code,
    position: num(s.position), avgCost: num(s.avg_cost),
    latestPrice: num(s.latest_price),
    realizedPnl: num(s.realized_pnl), unrealizedPnl: num(s.unrealized_pnl),
    totalPnl: num(s.total_pnl), fees: num(s.fees),
  };
}

export function toPnlView(res) {
  const rows = Array.isArray(res.per_code) ? res.per_code.map(toPnlRow) : [];
  const r = res.realized_summary || {};
  const u = res.unrealized_summary || {};
  return {
    rows,
    realized: { totalRealized: num(r.total_realized), totalFees: num(r.total_fees) },
    unrealized: { totalUnrealized: num(u.total_unrealized), totalValue: num(u.total_value),
                  holdingCount: num(u.holding_count) },
  };
}

export function toTradeRow(s) {
  return {
    id: s.id, date: s.trade_date, code: s.stock_code, name: s.stock_name || s.stock_code,
    direction: s.direction, directionText: DIR_TEXT[s.direction] || s.direction,
    price: num(s.price), quantity: num(s.quantity), amount: num(s.amount),
    settledAmount: num(s.settled_amount), source: s.source || "",
  };
}

export function toPreviewRow(s) {
  return toTradeRow({ ...s, id: null, source: "import" });
}

export function toSelectionJobRow(s) {
  return {
    jobId: s.job_id || s.id, strategies: parseStrList(s.strategies),
    status: s.status || "", source: s.source || "", error: s.error || "",
    resultCount: num(s.result_count), resultCountNow: num(s.result_count_now),
    createdAt: s.created_at || null,
  };
}

export function toSelectionResultRow(s) {
  return { code: s.code, name: s.name || s.code, signal: s.signal || "", reason: s.reason || "" };
}

export function toMessageRow(s) {
  return {
    id: s.id, eventCode: s.event_code || "", title: s.title || "", content: s.content || "",
    unread: s.read_status === true,     // 服务端 B10 _out：read_status = (read_at is None)，true=未读
    createdAt: s.created_at || null,
  };
}

export function toDiagRow(s) {
  return {
    runId: s.run_id, day: s.natural_day, status: s.status,
    holdingCount: num(s.held_code_count), actionCount: num(s.action_count),
    totalPnl: num(s.total_pnl),
  };
}

export function toUpgradeInfo(s) {
  return {
    hasUpdate: Boolean(s.has_update),
    latest: s.latest
      ? { versionCode: num(s.latest.version_code), versionName: s.latest.version_name || "",
          url: s.latest.url || "", md5: s.latest.md5 || "", sizeBytes: num(s.latest.size_bytes),
          releaseNotes: s.latest.release_notes || "", forced: Boolean(s.latest.forced),
          packageType: s.latest.package_type || "" }
      : null,
  };
}
```

- [ ] **Step 4: 运行确认通过**

Run: `npm test` → Expected: PASS（若 toMessageRow 语义需按 B10 `read_status` 精确化，以"可信源=服务端"收口，U10 斟酌）

- [ ] **Step 5: Commit**

```bash
git add uniapp/src/logic/view.js uniapp/tests/view.test.js
git commit -m "feat(uniapp): M6 零计算渲染映射（R1-3 仅透传）"
```

---

### Task U5: 交割单双步导入页（M7）

**Files:**
- Create: `uniapp/src/pages/holdings/import.vue`
- (页面走手动冒烟；`toPreviewRow` 已在 U4 覆盖映射断言)

- [ ] **Step 1: 页面实现（上传/粘贴 → 预览 → 提交）**

```vue
<!-- uniapp/src/pages/holdings/import.vue -->
<script setup>
import { ref } from "vue";
import { useClient } from "../../composables/useClient.js";
import { toPreviewRow } from "../../logic/view.js";

const mode = ref("text");          // text | file
const text = ref("");
const pickedPath = ref(null);
const preview = ref(null);          // { total, valid, errors:[{line,reason}], items:[] }
const submitting = ref(false);
const resultMsg = ref("");
const { trades } = useClient();

async function doPreview() {
  preview.value = null; resultMsg.value = "";
  const out = mode.value === "text"
    ? await trades.previewText(text.value)
    : await trades.previewFile(pickedPath.value, "file");
  preview.value = { ...out, viewRows: (out.items || []).map(toPreviewRow) };
}
function onPick(e) {
  const f = e.target?.files?.[0];
  if (f) { pickedPath.value = f; }   // H5: uni.uploadFile 支持 Blob/File；App: filePath 字符串
}
async function doCommit() {
  submitting.value = true;
  try {
    const res = await trades.commit(preview.value.items);
    resultMsg.value = `导入 ${res.imported} 条；持仓变更: ${(res.holdings_changed || []).join(", ") || "无"}`;
    preview.value = null;
    uni.showToast({ title: `导入 ${res.imported} 条` });
  } finally { submitting.value = false; }
}
</script>
<template>
  <view class="p">
    <view class="seg">
      <button :class="{ on: mode === 'file' }" @click="mode = 'file'">上传文件</button>
      <button :class="{ on: mode === 'text' }" @click="mode = 'text'">粘贴 CSV</button>
    </view>
    <textarea v-if="mode === 'text'" v-model="text" placeholder="粘贴券商交割单（成交日期,证券代码,…）" />
    <input v-else type="file" accept=".csv,.xlsx" @change="onPick" />
    <button @click="doPreview">预览解析</button>

    <view v-if="preview">
      <view class="bar">共 {{ preview.total }} 行，有效 {{ preview.valid }}，错误 {{ preview.errors.length }}</view>
      <view v-for="e in preview.errors" :key="e.line" class="err">第 {{ e.line }} 行：{{ e.reason }}</view>
      <view v-for="r in preview.viewRows" :key="r.date + r.code + r.direction + r.quantity" class="card">
        {{ r.date }} {{ r.name }}({{ r.code }}) {{ r.directionText }} {{ r.quantity }} @{{ r.price }}
      </view>
      <button :disabled="!preview.viewRows.length || submitting" @click="doCommit">确认提交</button>
    </view>
    <text v-if="resultMsg" class="ok">{{ resultMsg }}</text>
  </view>
</template>
```

- [ ] **Step 2: 手动冒烟（记录于 U13 README）**

> 特注：spec §8 中的 `logic/csv_deal.js`（本地交割单文本预览）为**可选**；本计划按"解析与口径全在后端"（spec §7.1）省略，前端只负责传输文件/文本与渲染服务端校验结果（YAGNI）。

- [ ] **Step 3: Commit**

```bash
git add uniapp/src/pages/holdings/import.vue
git commit -m "feat(uniapp): M7 交割单双步导入页（上传/粘贴→预览→提交）"
```

---

### Task U6: 持仓列表 + 编辑 + 利润分析页（M7）

**Files:**
- Create: `uniapp/src/pages/holdings/index.vue`、`uniapp/src/pages/holdings/edit.vue`、`uniapp/src/pages/profit/index.vue`

- [ ] **Step 1: 持仓页（列表 + 快照 + 导航）**

```vue
<!-- uniapp/src/pages/holdings/index.vue -->
<script setup>
import { ref } from "vue";
import { onShow } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";
import { toHoldingRow } from "../../logic/view.js";

const rows = ref([]); const loading = ref(false);
const { holdings } = useClient();
onShow(async () => {
  loading.value = true;
  try {
    const data = await holdings.list({});
    rows.value = (data.items || data || []).map(toHoldingRow);
  } finally { loading.value = false; }
});
const goEdit = (r) => uni.navigateTo({ url: `/pages/holdings/edit?code=${r?.code || ""}` });
const goImport = () => uni.navigateTo({ url: "/pages/holdings/import" });
const goProfit = () => uni.navigateTo({ url: "/pages/profit/index" });
const goTrades = () => uni.navigateTo({ url: "/pages/trades/index" });
</script>
<template>
  <view class="p">
    <view class="bar">
      <button size="mini" @click="goImport">交割单导入</button>
      <button size="mini" @click="goEdit()">新增持仓</button>
      <button size="mini" @click="goProfit">利润分析</button>
      <button size="mini" @click="goTrades">交易记录</button>
    </view>
    <view v-for="r in rows" :key="r.code" class="card" @click="goEdit(r)">
      <text>{{ r.name }}（{{ r.code }}）</text>
      <text>建议：{{ r.actionText }}</text>
      <text v-if="r.latestPrice !== null">现价 {{ r.latestPrice }} · 收益 {{ r.pnl }}（{{ r.pnlPct }}%）</text>
      <text class="reason">{{ r.actionReason }}</text>
    </view>
    <view v-if="!rows.length && !loading" class="empty">暂无持仓，点击「交割单导入」或「新增持仓」</view>
  </view>
</template>
```

> `edit.vue`：单只持仓编辑页，走 holdings CRUD：

```vue
<!-- uniapp/src/pages/holdings/edit.vue -->
<script setup>
import { ref } from "vue";
import { onLoad } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";

const code = ref(""); const name = ref(""); const quantity = ref(""); const costPrice = ref("");
const { holdings } = useClient();
onLoad((q) => { if (q.code) code.value = q.code; });

async function save() {
  const body = { code: code.value, name: name.value, quantity: Number(quantity.value),
                 cost_price: Number(costPrice.value) };
  if (code.value && quantity.value && costPrice.value) await holdings.create(body);
  uni.navigateBack();
}
</script>
<template>
  <view class="p">
    <input v-model="code" placeholder="6位代码" />
    <input v-model="name" placeholder="名称" />
    <input v-model="quantity" type="number" placeholder="数量" />
    <input v-model="costPrice" type="digit" placeholder="成本价" />
    <button :disabled="!code || !quantity || !costPrice" @click="save">保存</button>
  </view>
</template>
```

- [ ] **Step 2: 利润分析页（纯透传 /holdings/pnl）**

```vue
<!-- uniapp/src/pages/profit/index.vue -->
<script setup>
import { ref } from "vue";
import { onShow } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";
import { toPnlView } from "../../logic/view.js";

const v = ref({ rows: [], realized: {}, unrealized: {} });
const today = ref(""); const err = ref("");
const { holdings } = useClient();
onShow(async () => {
  today.value = new Date().toISOString().slice(0, 10);
  try {
    const res = await holdings.pnl({ natural_day: today.value });
    v.value = toPnlView(res);
  } catch (e) { err.value = e.message || "插件不可用"; }
});
</script>
<template>
  <view class="p">
    <view v-if="err" class="err">{{ err }}（由持仓诊断插件返回，前端不计算）</view>
    <view class="card">
      <text>未实现盈亏 {{ v.unrealized.totalUnrealized }} · 持仓市值 {{ v.unrealized.totalValue }}</text>
      <text>已实现盈亏 {{ v.realized.totalRealized }} · 费用 {{ v.realized.totalFees }}</text>
    </view>
    <view v-for="r in v.rows" :key="r.code" class="card">
      <text>{{ r.name }}（{{ r.code }}）仓位 {{ r.position }} 成本 {{ r.avgCost }} 现价 {{ r.latestPrice }}</text>
      <text>已实现 {{ r.realizedPnl }} · 浮动 {{ r.unrealizedPnl }} · 合计 {{ r.totalPnl }}</text>
    </view>
    <view v-if="!v.rows.length" class="empty">暂无持仓数据</view>
  </view>
</template>
```

- [ ] **Step 3: Commit**

```bash
git add uniapp/src/pages/holdings/index.vue uniapp/src/pages/holdings/edit.vue uniapp/src/pages/profit/index.vue
git commit -m "feat(uniapp): M7 持仓列表/编辑 + 利润分析页（透传插件）"
```

---

### Task U7: 交易记录页（筛选 + 手动录入 + 删除）（M7）

**Files:**
- Create: `uniapp/src/pages/trades/index.vue`

- [ ] **Step 1: 页面实现**

```vue
<!-- uniapp/src/pages/trades/index.vue -->
<script setup>
import { ref } from "vue";
import { onShow } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";
import { toTradeRow } from "../../logic/view.js";

const rows = ref([]);
const filter = ref({ code: "", direction: "" });
const showAdd = ref(false);
const frm = ref({ trade_date: new Date().toISOString().slice(0, 10), stock_code: "", stock_name: "",
                   direction: "buy", price: "", quantity: "", commission: "0", stamp_tax: "0", transfer_fee: "0" });
const { trades } = useClient();

async function reload() {
  const q = {};
  if (filter.value.code) q.code = filter.value.code;
  if (filter.value.direction) q.direction = filter.value.direction;
  const data = await trades.list(q);
  rows.value = (data.items || []).map(toTradeRow);
}
onShow(reload);

async function doAdd() {
  const body = { ...frm.value, price: Number(frm.value.price), quantity: Number(frm.value.quantity),
                 amount: 0, settledAmount: 0, commission: Number(frm.value.commission),
                 stamp_tax: Number(frm.value.stamp_tax), transfer_fee: Number(frm.value.transfer_fee) };
  await trades.create(body);
  showAdd.value = false; reload();
  uni.showToast({ title: "已录入并更新持仓" });
}
async function del(r) {
  if (r.source !== "manual") { uni.showToast({ title: "导入记录不可单笔删除", icon: "none" }); return; }
  uni.showModal({ title: "删除记录", content: `删除 ${r.name} ${r.directionText} ${r.quantity}？`,
    success: async (o) => { if (o.confirm) { await trades.remove(r.id); reload(); } } });
}
</script>
<template>
  <view class="p">
    <view class="bar">
      <input v-model="filter.code" placeholder="代码" />
      <picker :range="['', 'buy', 'sell']" @change="filter.direction = $event.detail.value">
        <text>{{ filter.direction || '方向' }}</text>
      </picker>
      <button size="mini" @click="reload">查询</button>
      <button size="mini" @click="showAdd = !showAdd">手动录入</button>
    </view>
    <view v-if="showAdd" class="form">
      <input v-model="frm.stock_code" placeholder="6位代码" />
      <input v-model="frm.stock_name" placeholder="名称" />
      <picker :range="['buy', 'sell']" :value="frm.direction === 'sell' ? 1 : 0"
              @change="frm.direction = $event.detail.value">
        <text>{{ frm.direction === 'sell' ? '卖出' : '买入' }}</text>
      </picker>
      <input v-model="frm.price" type="digit" placeholder="价格" />
      <input v-model="frm.quantity" type="number" placeholder="数量" />
      <input v-model="frm.trade_date" placeholder="YYYY-MM-DD" />
      <button :disabled="!frm.stock_code || !frm.price || !frm.quantity" @click="doAdd">提交</button>
    </view>
    <view v-for="r in rows" :key="r.id" class="card">
      <text>{{ r.date }} {{ r.name }}（{{ r.code }}）</text>
      <text>{{ r.directionText }} {{ r.quantity }} @ {{ r.price }} · 结算 {{ r.settledAmount }}</text>
      <button size="mini" @click="del(r)">删除</button>
    </view>
    <view v-if="!rows.length" class="empty">暂无交易记录</view>
  </view>
</template>
```

- [ ] **Step 2: Commit**

```bash
git add uniapp/src/pages/trades
git commit -m "feat(uniapp): M7 交易记录页（筛选/手动录入/删除）"
```

---

### Task U8: 选股页（触发 + 轮询 + 结果）+ 历史回看（M8）

**Files:**
- Create: `uniapp/src/pages/selection/index.vue`、`uniapp/src/pages/selection/history.vue`

- [ ] **Step 1: 选股页**

```vue
<!-- uniapp/src/pages/selection/index.vue -->
<script setup>
import { ref } from "vue";
import { onShow, onUnload } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";
import { toSelectionResultRow } from "../../logic/view.js";

const { selection } = useClient();
const strategies = ref([]);
const chosen = ref([]);
const job = ref(null);          // { jobId, status, results, ... }
const results = ref([]);
const polling = ref(false);
const err = ref("");
let timer = null;

onShow(async () => {
  if (!strategies.value.length) {
    try { strategies.value = await selection.strategies(); } catch (e) { err.value = e.message || "选股插件不可用"; }
  }
});
onUnload(() => { clearInterval(timer); timer = null; });

async function trigger() {
  err.value = "";
  const out = await selection.run(chosen.value.length ? chosen.value : null);
  job.value = { jobId: out.job_id, status: out.status, results: [] };
  startPoll(out.job_id);
}
function startPoll(jobId) {
  polling.value = true;
  timer = setInterval(async () => {
    try {
      const d = await selection.runDetail(jobId);
      job.value = { jobId: d.job_id, status: d.status, strategies: d.strategies, results: d.results || [] };
      results.value = (d.results || []).map(toSelectionResultRow);
      if (d.status === "done" || d.status === "failed") { clearInterval(timer); timer = null; polling.value = false; }
    } catch (e) { clearInterval(timer); timer = null; polling.value = false; err.value = e.message; }
  }, 2000);
}
const goHistory = () => uni.navigateTo({ url: "/pages/selection/history" });
</script>
<template>
  <view class="p">
    <view v-for="s in strategies" :key="s" class="chip" :class="{ on: chosen.includes(s) }"
          @click="chosen = chosen.includes(s) ? chosen.filter(x => x !== s) : [...chosen, s]">{{ s }}</view>
    <button :disabled="polling" @click="trigger">{{ polling ? "选股中…" : "开始选股" }}</button>
    <button size="mini" @click="goHistory">历史回看</button>
    <text v-if="err" class="err">{{ err }}</text>
    <view v-for="r in results" :key="r.code" class="card">
      <text>{{ r.name }}（{{ r.code }}）信号：{{ r.signal }}</text>
      <text class="reason">{{ r.reason }}</text>
    </view>
    <view v-if="job && !results.length && job.status !== 'done'" class="empty">任务 {{ job.status }}…</view>
  </view>
</template>
```

- [ ] **Step 2: 历史回看页**

```vue
<!-- uniapp/src/pages/selection/history.vue -->
<script setup>
import { ref } from "vue";
import { onShow } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";
import { toSelectionJobRow } from "../../logic/view.js";

const runs = ref([]); const expanded = ref(null); const detailResults = ref([]);
const { selection } = useClient();
onShow(async () => {
  const data = await selection.runs({ page: 1, size: 50 });
  runs.value = (data.items || []).map(toSelectionJobRow);
});
async function toggle(j) {
  expanded.value = expanded.value === j.jobId ? null : j.jobId;
  if (expanded.value) {
    const d = await selection.runDetail(j.jobId);
    detailResults.value = (d.results || []).map((r) => r);
  }
}
</script>
<template>
  <view class="p">
    <view v-for="j in runs" :key="j.jobId" class="card" @click="toggle(j)">
      <text>{{ j.createdAt || '' }} · {{ j.status }} · {{ j.strategies.join('/') }} · {{ j.resultCount }} 条</text>
      <view v-if="expanded === j.jobId">
        <view v-for="r in detailResults" :key="r.code">{{ r.name }}（{{ r.code }}）{{ r.signal }}</view>
      </view>
    </view>
    <view v-if="!runs.length" class="empty">暂无选股记录</view>
  </view>
</template>
```

- [ ] **Step 3: Commit**

```bash
git add uniapp/src/pages/selection
git commit -m "feat(uniapp): M8 选股页（策略/触发/轮询/结果）+ 历史回看"
```

---

### Task U9: 诊断回看页（M9）

**Files:**
- Create: `uniapp/src/pages/diag/index.vue`、`uniapp/src/pages/diag/detail.vue`

- [ ] **Step 1: 历史 + 明细页**

```vue
<!-- uniapp/src/pages/diag/index.vue -->
<script setup>
import { ref } from "vue";
import { onShow } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";
import { toDiagRow } from "../../logic/view.js";
import { loadSession } from "../../store/session.js";

const rows = ref([]);
const { client } = useClient();
onShow(async () => {
  const me = loadSession().me || {};
  const data = await client.get("/api/portfolio/runs", { query: { user_id: me.id } });
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
    <view v-if="!rows.length" class="empty">暂无诊断记录（由定时任务产生）</view>
  </view>
</template>
```

```vue
<!-- uniapp/src/pages/diag/detail.vue —— 明细字段全部来自服务端 -->
<script setup>
import { ref } from "vue";
import { onLoad } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";
import { toHoldingRow } from "../../logic/view.js";

const results = ref([]); const err = ref("");
const { client } = useClient();
onLoad(async (q) => {
  try {
    const data = await client.get(`/api/external/portfolio/diag/${q.runId}`);
    results.value = (data.results || []).map(toHoldingRow);
  } catch (e) { err.value = e.message; }
});
</script>
<template>
  <view class="p">
    <view v-for="r in results" :key="r.code" class="card">
      <text>{{ r.name }}（{{ r.code }}）建议：{{ r.actionText }}</text>
      <text>现价 {{ r.latestPrice }} · 盈亏 {{ r.pnl }}（{{ r.pnlPct }}%）</text>
      <text class="reason">{{ r.actionReason }}</text>
    </view>
    <view v-if="err" class="err">{{ err }}</view>
  </view>
</template>
```

- [ ] **Step 2: Commit**

```bash
git add uniapp/src/pages/diag
git commit -m "feat(uniapp): M9 诊断回看页（历史 run + 明细）"
```

---

### Task U10: WS 状态机（纯逻辑）+ 消息中心页（M9）

**Files:**
- Create: `uniapp/src/logic/ws.js`
- Create: `uniapp/tests/ws.test.js`
- Create: `uniapp/src/pages/messages/index.vue`

- [ ] **Step 1: 写失败测试**

```js
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
```

- [ ] **Step 2: 运行确认失败**

Run: `npm test` → Expected: FAIL

- [ ] **Step 3: 实现 ws.js**

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

  function send(obj) {
    state.socket?.send?.(JSON.stringify(obj));
  }

  function handleFrame(ev) {
    let msg;
    try { msg = JSON.parse(ev?.data); } catch { return; }
    if (msg.type === "hello") {
      onReconnect({ sinceId: msg.latest_inbox_id ?? null, unreadCount: msg.unread_count ?? 0 });
      return;
    }
    if (msg.type === "msg") { onMessage(msg); send({ type: "pong" }); return; }
    if (msg.type === "ping") send({ type: "pong" });
  }

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
    on("onmessage", "onMessage", handleFrame);
    on("onclose", "onClose", () => { state.connected = false; scheduleReconnect(); });
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

- [ ] **Step 4: 运行确认通过**

Run: `npm test` → Expected: PASS

- [ ] **Step 5: 消息中心页（列表 + 已读 + WS 实时 + 未读数）**

```vue
<!-- uniapp/src/pages/messages/index.vue -->
<script setup>
import { ref } from "vue";
import { onShow, onUnload } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";
import { createNotifySocket } from "../../logic/ws.js";
import { toMessageRow } from "../../logic/view.js";
import { loadSession } from "../../store/session.js";

const rows = ref([]); const unread = ref(0); const loaded = ref(false);
const { messages } = useClient();
const WS_URL = globalThis.__VITE_WS_URL__ || "ws://127.0.0.1:8000/ws/notify";
let sock = null;

async function reload(sinceId) {
  const q = { page: 1, size: 50 };
  if (sinceId) q.since_id = sinceId;
  const data = await messages.list(q);
  rows.value = (data.items || []).map(toMessageRow);
  unread.value = data.unread_count ?? 0;
  loaded.value = true;
}

function ensureWs() {
  if (sock) return;
  sock = createNotifySocket({
    url: WS_URL,
    token: loadSession().token,
    onReconnect: ({ sinceId }) => reload(sinceId || undefined),   // hello 即按 latest_inbox_id 补齐
    onMessage: () => reload(),                                    // 新消息帧 → 刷新
  });
  sock.connect();
}

onShow(() => { reload(); ensureWs(); });
onUnload(() => { sock?.close(); sock = null; });

async function read(m) {
  if (!m.unread) return;
  await messages.markRead(m.id);
  reload();
}
async function readAll() {
  await messages.markAllRead();
  reload();
}
</script>
<template>
  <view class="p">
    <view class="bar">
      <text>未读 {{ unread }}</text>
      <button size="mini" @click="readAll">全部已读</button>
    </view>
    <view v-for="m in rows" :key="m.id" class="card" @click="read(m)">
      <text :class="{ unread: m.unread }">{{ m.title }}</text>
      <text class="reason">{{ m.content }}</text>
    </view>
    <view v-if="loaded && !rows.length" class="empty">暂无消息</view>
  </view>
</template>
```

- [ ] **Step 6: Commit**

```bash
git add uniapp/src/logic/ws.js uniapp/tests/ws.test.js uniapp/src/pages/messages
git commit -m "feat(uniapp): M9 消息中心 + WS 实时（hello对齐/msg刷新/已读）"
```

---

### Task U11: 自动更新（H5 检查 + App uni-upgrade-app 逻辑）（M10）

**Files:**
- Create: `uniapp/src/logic/version.js`
- Create: `uniapp/tests/version.test.js`
- Modify: `uniapp/src/App.vue`、`uniapp/src/pages/mine/index.vue`

- [ ] **Step 1: 写失败测试**

```js
// uniapp/tests/version.test.js
import test from "node:test";
import assert from "node:assert/strict";
import { checkUpdate } from "../src/logic/version.js";

test("远程新于本地 → has_update true，且 forced 透传", () => {
  const info = checkUpdate({ local: 100, remote: { version_code: 102, forced: true } });
  assert.equal(info.hasUpdate, true);
  assert.equal(info.forced, true);
});

test("本地已最新 → has_update false", () => {
  assert.equal(checkUpdate({ local: 102, remote: { version_code: 102 } }).hasUpdate, false);
});

test("远程为空 → has_update false", () => {
  assert.equal(checkUpdate({ local: 100, remote: null }).hasUpdate, false);
});

test("H5 忽略下载包字段（无 url 也可提示刷新）", () => {
  const info = checkUpdate({ local: 100, remote: { version_code: 101, package_type: "h5" } });
  assert.equal(info.hasUpdate, true);
  assert.equal(info.packageType, "h5");
});
```

- [ ] **Step 2: 运行确认失败**

Run: `npm test` → Expected: FAIL

- [ ] **Step 3: 实现 version.js**

```js
// uniapp/src/logic/version.js —— 版本比对纯函数（R1-3 允许的元信息判定）
export function checkUpdate({ local, remote }) {
  if (!remote) return { hasUpdate: false, latest: null, forced: false, packageType: "" };
  const rc = Number(remote.version_code) || 0;
  return {
    hasUpdate: rc > Number(local || 0),
    latest: remote,
    forced: Boolean(remote.forced),
    packageType: remote.package_type || "",
  };
}
```

- [ ] **Step 4: 运行确认通过 + 接线**

接入 `App.vue`（H5 启动检查）与 `mine/index.vue`（手动检查 + App 端 upgrade 逻辑）：

```js
// App.vue —— onLaunch 追加（先登录检查，再被动版本检查）
import { useClient } from "./composables/useClient.js";
import { checkUpdate } from "./logic/version.js";
import { loadSession } from "./store/session.js";

function h5VersionCheck() {
  const local = Number(globalThis.__H5_VER_CODE__ || 100);   // 构建注入，缺省随 manifest
  useClient().upgrade.latest({ platform: "h5", version_code: local })
    .then((info) => {
      const u = checkUpdate({ local, remote: info });
      if (u.hasUpdate) {
        uni.showModal({ title: "版本更新", content: u.latest?.release_notes || "发现新版本",
          confirmText: "刷新", success: (o) => { if (o.confirm) uni.reLaunch({ url: "/pages/holdings/index" }); } });
      }
    }).catch(() => {});
}
```

```js
// pages/mine/index.vue 增强：+ 检查更新 / 修改密码入口 / 退出
// #ifdef APP-PLUS
import { checkUpdate } from "../../logic/version.js";
function appUpdate() {
  const local = Number(globalThis.__APP_VER_CODE__ || 100);
  useClient().upgrade.latest({ platform: (isAndroidApp() ? "android" : "ios"), version_code: local })
    .then((info) => {
      const u = checkUpdate({ local, remote: info });
      if (!u.hasUpdate) { uni.showToast({ title: "已是最新版本" }); return; }
      const url = u.latest?.url || ""; const md5 = u.latest?.md5 || "";
      if (!url) { uni.showToast({ title: "暂无可下载包", icon: "none" }); return; }
      uni.showModal({ title: "发现新版本" + (u.latest?.version_name || ""),
        content: u.latest?.release_notes || "", showCancel: !u.forced,   // forced 不可关闭（强制升级）
        success: (o) => { if (o.confirm || u.forced) downloadAndInstall(url, md5, u.packageType); } });
    });
}
function downloadAndInstall(url, md5, pkgType) {
  const dtask = plus.downloader.createDownload(url, { filename: "_doc/update/" + url.split("/").pop() },
    (d, status) => {
      if (status === 200) {
        const path = d.filename;
        if (pkgType === "wgt") plus.runtime.install(path, { force: true }, () => uni.showToast({ title: "热更完成，重启生效" }));
        else plus.runtime.install(path, { force: true });   // apk/ipa 整包
      } else uni.showToast({ title: "下载失败", icon: "none" });
    });
  dtask.start();
}
// #endif
```

> 说明：`__H5_VER_CODE__`/`__APP_VER_CODE__` 与 manifest `versionCode` 同步；H5 发布是构建产物整体发布（`/api/app/latest?platform=h5` 仅提示），App 端 wgt 热更走本逻辑；iOS 原则上仅发布 wgt。手动冒烟记录于 U13。

- [ ] **Step 5: Commit**

```bash
git add uniapp/src/logic/version.js uniapp/tests/version.test.js uniapp/src/App.vue uniapp/src/pages/mine
git commit -m "feat(uniapp): M10 自动更新（H5 检查 + App uni-upgrade-app 逻辑/强制升级）"
```

---

### Task U12: 端到端联调（M11）

**Files:**
- Create: `scripts/samples/sample_deal.csv`
- 联调期按需修正各页面

- [ ] **Step 1: 准备样例交割单**

```csv
# scripts/samples/sample_deal.csv（不含注释行；解析器以首行为表头，注释行会误当表头）
成交日期,证券代码,证券名称,买卖标志,成交数量,成交价格,成交金额,手续费,印花税,过户费,结算金额
2026-09-01,600519,贵州茅台,买入,100,1680.50,168050.00,5.00,168.05,0.50,168223.55
2026-09-01,000858,五粮液,买入,200,142.00,28400.00,3.00,28.40,0.00,28431.40
2026-09-02,000001,平安银行,买入,1000,11.20,11200.00,2.50,11.20,0.00,11213.70
```

- [ ] **Step 2: 三服务启动与冒烟**

```bash
# 终端1 backend
cd stock_selector_api/backend && python -m uvicorn main:app --port 8000
# 终端2 持仓诊断插件
cd stock_selector_api/plugin && python -m uvicorn server:app --port 8001
# 终端3 选股插件
bash scripts/start_selector.sh
# 终端4 调度器（如需定时验证）
python -m scheduler --conf conf/inputs.conf
```

- [ ] **Step 3: 手动链路清单（执行并勾选）**

- [ ] H5 `npm run dev:h5` 登录成功进入持仓 Tab
- [ ] 交割单导入：选择 `scripts/samples/sample_deal.csv` → 预览 3 条有效 0 错 → 提交 → 持仓出现 3 只，数量/成本正确
- [ ] 利润分析页显示 每代码/已实现/未实现 汇总（全部来自 8001 插件，无前端计算）
- [ ] 交易记录页显示 3 条导入记录（source=import）；手动录入 1 笔 buy 后持仓联动（数量增加），该笔可删除
- [ ] 选股页：拉取策略列表 → 触发 → 轮询到 done → 渲染结果；WS/站内信收到 `selection_completed`
- [ ] 消息页：未读数正确；切到别的 Tab，后端再触发一次选股/诊断 → 在线收到 msg 帧实时刷新；重启页面走 since_id 补齐
- [ ] 诊断页显示最近 run，明细与后端一致
- [ ] 定时：启动 scheduler 后观察到 `daily_selection` 自动 job 与通知（时间可临时调近验证）
- [ ] 升级：管理端上传 h5 包并发布新 `version_code` → H5 刷新弹「版本更新」；App 端发布 wgt 包 → 强制升级不可关闭、安装成功
- [ ] 桌面宽度下页面容器可读（PC 自适应收口）

- [ ] **Step 4: Commit（联调修正）**

```bash
git add -A
git commit -m "chore(uniapp): M11 端到端联调（三服务联动/交割单全链路/通知/升级）"
```

---

### Task U13: README + 部署文档 + 双端冒烟清单（M11）

**Files:**
- Create: `uniapp/README.md`
- Create: `docs/deploy/2026-09-20-app.md`

- [ ] **Step 1: README 内容**

```markdown
# 智能选股 UniApp 客户端
## 运行（H5，开发）
1. `npm install`（离线则跳过，纯逻辑测试仍可跑）
2. 三服务：backend 8000 / plugin 8001 / selector 8002（见 docs/deploy）
3. `npm run dev:h5` → 打开本地地址（/api、/ws 代理 8000）
## 纯逻辑测试（无需安装依赖）
`npm test`（node --test：scaffold/guard/client/view/ws/version）
## 构建
- `npm run build:h5` → dist/build/h5（整体发布）
- `npm run build:app` → HBuilderX 云打包或本地打包；wgt 使用「制作wgt资源」
## 手动冒烟清单（双端）
- [ ] 未登录访问任一 Tab → 登录页；登录成功进持仓；退出回登录
- [ ] 交割单双步导入（上传/粘贴 → 预览行级错误 → 提交）持仓联动
- [ ] 利润/持仓/选股/诊断：全部数值与后端/插件一致，页面无计算
- [ ] 消息未读 + WS 实时 + 断线重连按 latest_inbox_id 补齐
- [ ] H5 发布新版刷新可见；App wgt 热更/整包安装；forced 不可跳过
- [ ] 修改密码页按后端既有接口接线（无则记录为已知项）
## 风险
- 离线装包：结构+纯逻辑可验证，UI 打包留待联网
```

- [ ] **Step 2: 部署文档内容（节选）**

```markdown
# 2026-09-20-app.md
## 服务启动
- backend: cd stock_selector_api/backend && python -m uvicorn main:app --port 8000
- plugin(8001): cd stock_selector_api/plugin && python -m uvicorn server:app --port 8001
- selector(8002): bash scripts/start_selector.sh
- scheduler: cd stock_selector_api/backend && python -m scheduler --conf conf/inputs.conf
## 首次初始化
- SQLite 自动建表（data/app.db）；老库 holdings 自动补列
- 建管理员与普通用户；RBAC 权限由既有 seed 配置
## H5 发布流程
1. npm run build:h5 2) 上传制品到静态服务器 3) 管理端 POST /api/v1/upgrade/packages（platform=h5, 新 version_code, is_published=true）
## App 发布（wgt 热更 / 整包）
1. build:app 打 wgt 或 apk/ipa 2) 管理端「上传」得到 url+md5+size 3) 建 upgrade_packages（platform=android|ios, package_type, version_code 严格递增, forced 可设）
4) 客户端 /api/app/latest 比对 version_code 触发更新
## 定时任务
- daily_diag 18:00、daily_selection 17:30（conf/inputs.conf，可改时间）
## 升级中心说明
- 自托管等价 uni-upgrade-center：后端 upgrade_packages + /api/app/latest；不依赖 uniCloud
## 端口
- backend 8000 / plugin 8001 / selector 8002；全市场选股较重，手动触发为 job+轮询
```

- [ ] **Step 3: Commit**

```bash
git add uniapp/README.md docs/deploy/2026-09-20-app.md
git commit -m "docs(uniapp): M11 README + 部署文档 + 双端冒烟清单"
```

---

## 验收对照（uniapp M6–M11）

| 规格需求 | 任务 | 验证 |
|---|---|---|
| 5 Tab 壳层 + 登录隔离（含 428） | U1/U2 | setup-smoke / guard.test.js |
| API 客户端（token/401/428/上传/raw）+ 领域 API | U3 | client.test.js |
| 前端零计算（R1-3） | U4 | view.test.js 透传断言 + 代码审查 |
| 交割单双步导入 + 持仓/交易/利润页 | U5/U6/U7 | 页面 + 手动冒烟 + view 映射 |
| 选股（手动触发+轮询+历史） | U8 | 页面 + 手动冒烟 |
| 诊断回看 | U9 | 页面 + 手动冒烟 |
| 消息中心 + WS 实时（对齐/退避） | U10 | ws.test.js + 手动冒烟 |
| 自动更新（H5/App/强制） | U11 | version.test.js + 手动冒烟 |
| 端到端联调 + 部署 + 冒烟 | U12/U13 | 联调清单 |

## 风险与注意

- **离线装包**：`npm install` 依赖外网；离线时 U1 结构 + U2–U4/U10 纯逻辑仍可完整验证，UI 打包留待联网（README 记录，继承 M5 做法）。
- **契约漂移**：以 backend 计划 B-任务为准；发现不符 → uni 端先适配，必要时回写 backend 计划并同步 spec。
- **App 打包/真机差异**：`plus.downloader/runtime.install` 与 uni 运行时差异不纳入自动化；以手工冒烟覆盖；iOS 仅 wgt。
- **修改密码页**：改密接口语义未在 backend 计划中显式扩展；U12 联调核对现有能力，若无则缺省"提示 + 重新登录"（URN01），不阻塞本轮交付。
- **全市场选股耗时**：选股页用 job+轮询（每 2s），不得同步阻塞 UI；定时任务单飞由后端 B13 保证。
- **强制升级**：`forced=true` 时 UI 不提供跳过（`showCancel: !forced`），违反视为 B 类缺陷。

## 全计划任务计数

- backend 计划 = B1–B19（19 任务）
- uniapp 计划 = U1–U13（13 任务）
- **合计 = 32 任务 ≤ spec 第 10 节 32 上限**。执行顺序建议 backend 先行（契约落定），uniapp 的 U1–U4 可并行起步。