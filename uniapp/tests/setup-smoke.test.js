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
  assert.deepEqual(cfg.tabBar.list.map((t) => t.text), ["持仓", "选股", "资金", "消息", "我的"]);
});