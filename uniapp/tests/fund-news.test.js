// tests/fund-news.test.js —— 资金榜/消息流的展示层：量纲换算、空态文案、页面与路由表一致性
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { toFundRow, fundHeadline, toNewsFeedRow } from "../src/logic/view.js";
import { fundApi } from "../src/api/fund.js";
import { newsApi } from "../src/api/news.js";
import { PAGE_PATHS, TAB_PATHS, resolveRoute } from "../src/logic/guard.js";

// ---------------------------------------------------------------- 资金榜换算

test("toFundRow：元换算成亿、带符号，占比给百分数", () => {
  const r = toFundRow({ code: "600000", name: "浦发银行", close: 9.2,
                        net_inflow: 312000000, ratio: 0.153, ret_pct: 4.21, days: 3 });
  assert.equal(r.netText, "+3.12亿");
  assert.equal(r.ratioText, "+15.30%");
  assert.equal(r.retText, "+4.21%");
  assert.equal(r.days, 3);
  assert.equal(r.isStock, true);
});

test("toFundRow：净流出保留负号，缺值显示「—」而不是假装 0", () => {
  const out = toFundRow({ code: "000001", name: "平安银行", net_inflow: -200000000,
                          ratio: -0.2, ret_pct: -3, days: 7 });
  assert.equal(out.netText, "-2.00亿");
  assert.equal(out.ratioText, "-20.00%");
  assert.equal(toFundRow({ code: "1", net_inflow: null }).netText, "—");
});

test("fundHeadline：档位缺数时先把后端的 why 念出来，再说总票数", () => {
  const ok = { asof: "2026-10-29", window: 3, total: 5373, count: 50, rows: [{}], notice: null };
  assert.equal(fundHeadline(ok).text, "截至 2026-10-29 · 3 个交易日 · 全市场 5373 只");
  assert.equal(fundHeadline(ok).cls, "");

  const empty = { asof: "2026-10-29", window: 30, total: 0, count: 0, rows: [],
                  notice: "30 日档没有票：窗口内有缺数日（2026-10-16 只采到 186 行）" };
  assert.match(fundHeadline(empty).text, /2026-10-16/);
  assert.equal(fundHeadline(empty).cls, "bad");

  const missing = { asof: null, rows: [], total: 0, count: 0, notice: null };
  assert.equal(fundHeadline(missing).text, "资金榜还没有数据");
  assert.equal(fundHeadline(missing).cls, "muted");
});

// ---------------------------------------------------------------- 消息流行

test("toNewsFeedRow：stockList 拆成个股码与板块码，标题摘要原样透出", () => {
  const r = toNewsFeedRow({ id: "6ae3ef969ca5d4a7d72421b8", title: "人民锐评",
                            summary: "关于中美人工智能发展……", showTime: "2026-10-30 09:13:28",
                            stockList: ["90.BK0800", "1.600000", "0.161631"] });
  assert.equal(r.title, "人民锐评");
  assert.equal(r.time, "10-30 09:13");
  assert.deepEqual(r.stocks, ["600000", "161631"]);
  assert.deepEqual(r.boards, ["BK0800"]);
});

test("toNewsFeedRow：没有关联标的也给空数组，页面据此不画标签", () => {
  const r = toNewsFeedRow({ id: "x", title: "t", summary: "", showTime: "", stockList: null });
  assert.deepEqual(r.stocks, []);
  assert.deepEqual(r.boards, []);
  assert.equal(r.summary, "");
});

// ---------------------------------------------------------------- 接口路径

test("api 路径与 8010 后端一致", async () => {
  const calls = [];
  const c = { get: (url, opt) => { calls.push([url, opt]); return Promise.resolve({}); } };
  const fund = fundApi(c);
  const news = newsApi(c);
  await fund.rank({ window: 7, side: "out", limit: 30 });
  await news.feed({ limit: 20, before: "abc" });
  await news.item("6ae3ef969ca5d4a7d72421b8");
  assert.deepEqual(calls, [
    ["/api/v1/fund/rank", { query: { window: 7, side: "out", limit: 30 } }],
    ["/api/v1/news/feed", { query: { limit: 20, before: "abc" } }],
    ["/api/v1/news/item", { query: { id: "6ae3ef969ca5d4a7d72421b8" } }],
  ]);
});

// ---------------------------------------------------------------- 页面与守卫

test("pages.json：新增三个页面，诊断不再是 tab", () => {
  const cfg = JSON.parse(readFileSync(new URL("../src/pages.json", import.meta.url)));
  const paths = cfg.pages.map((p) => p.path);
  for (const p of ["pages/fund/index", "pages/news/feed", "pages/news/detail"])
    assert.ok(paths.includes(p), `缺页面 ${p}`);
  assert.ok(!cfg.tabBar.list.some((t) => t.pagePath === "pages/diag/index"), "诊断不该还在 tab 上");
});

test("守卫表与 pages.json 一致，诊断仍可作为非 tab 页打开", () => {
  const cfg = JSON.parse(readFileSync(new URL("../src/pages.json", import.meta.url)));
  const paths = new Set(cfg.pages.map((p) => p.path));
  for (const p of PAGE_PATHS) assert.ok(paths.has(p), `守卫表里的 ${p} 在 pages.json 不存在`);
  for (const t of cfg.tabBar.list) assert.ok(TAB_PATHS.includes(t.pagePath), `${t.pagePath} 未进 TAB_PATHS`);
  assert.deepEqual(TAB_PATHS, cfg.tabBar.list.map((t) => t.pagePath));
  assert.ok(PAGE_PATHS.includes("pages/diag/index"));
  assert.equal(resolveRoute({ path: "pages/diag/index", loggedIn: true }).ok, true);
  assert.equal(resolveRoute({ path: "pages/fund/index", loggedIn: true }).ok, true);
});

test("诊断从 tab 退下来后，「我的」页要留一个入口，否则页面就找不到了", () => {
  const src = readFileSync(new URL("../src/pages/mine/index.vue", import.meta.url), "utf8");
  assert.match(src, /pages\/diag\/index/, "「我的」页缺少诊断入口");
});
