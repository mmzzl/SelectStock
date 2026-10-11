import test from "node:test";
import assert from "node:assert/strict";
import { toHoldingRow, toTradeRow, toPnlView, toSelectionJobRow, toSelectionResultRow,
         toMessageRow, toDiagRow, toPreviewRow, toUpgradeInfo, fmtTime,
         riskText, riskClass } from "../src/logic/view.js";

test("时间格式化：naive UTC 转本地、压到分钟；带时区/非法值不崩", async () => {
  const { execFileSync } = await import("node:child_process");
  const expect = execFileSync(process.execPath, ["-e",
    "const d=new Date(Date.parse('2026-09-24T11:19:38.450105Z'));" +
    "const p=n=>String(n).padStart(2,'0');" +
    "process.stdout.write(`${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`);",
  ], { encoding: "utf8" });
  assert.equal(fmtTime("2026-09-24T11:19:38.450105"), expect);
  assert.equal(fmtTime("2026-09-24 11:19:38"), expect);   // 空格分隔同样按 UTC 解析
  assert.equal(fmtTime("2026-09-24T19:19:38+08:00"), expect);  // 已带时区不再补 Z
  assert.equal(fmtTime(""), "");
  assert.equal(fmtTime("不是时间"), "不是时间");
});

test("持仓行：快照字段透传，不重算", () => {
  const s = { code: "600519", name: "贵州茅台", cost_price: 1680.5, quantity: 100,
              action: "sell", action_reason: "跌破MA10", latest_price: 1500,
              holding_pnl: -18050, pnl_pct: -10.7, diag_run_id: "r9" };
  const row = toHoldingRow(s);
  assert.equal(row.cost, 1680.5);
  assert.equal(row.shares, 100);
  assert.equal(row.latestPrice, 1500);
  assert.equal(row.pnl, -18050);
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
  assert.equal(toMessageRow({ id: "m1", read_status: false }).unread, false);
  assert.equal(toMessageRow({ id: "m2", read_status: true }).unread, true);
});

test("版本信息：透传并标注更新判定字段", () => {
  const info = toUpgradeInfo({ has_update: true, latest: { version_code: 102, version_name: "1.0.2",
    url: "/u/a.wgt", md5: "m", release_notes: "修复", forced: true, package_type: "wgt" } });
  assert.equal(info.hasUpdate, true);
  assert.equal(info.latest.forced, true);
});

test("预览记录行：置 id=null、source=import", () => {
  const s = { id: "x", trade_date: "2026-09-01", stock_code: "600519", stock_name: "茅台",
              direction: "buy", price: 1680.5, quantity: 100, settled_amount: 168050, source: "old" };
  const row = toPreviewRow(s);
  assert.equal(row.id, null);
  assert.equal(row.source, "import");
  assert.equal(row.directionText, "买入");
});

test("诊断行：字段映射透传", () => {
  const s = { run_id: "r1", natural_day: "2026-09-01", status: "done",
              held_code_count: 3, action_count: 1, total_pnl: -5000 };
  const row = toDiagRow(s);
  assert.equal(row.runId, "r1");
  assert.equal(row.day, "2026-09-01");
  assert.equal(row.status, "done");
  assert.equal(row.holdingCount, 3);
  assert.equal(row.actionCount, 1);
  assert.equal(row.totalPnl, -5000);
});

test("选股结果行：字段映射透传", () => {
  const row = toSelectionResultRow({ code: "600519", name: "茅台", signal: "trend_rebound", reason: "MACD金叉" });
  assert.deepEqual(row, {
    code: "600519", name: "茅台", signal: "trend_rebound", reason: "MACD金叉",
    strategy: "trend_rebound", asof: "", price: null, score: null, rank: null, risk: null,
  });
});

test("选股结果行：插件富化字段落到 camelCase", () => {
  const row = toSelectionResultRow({
    code: "000001", name: "平安银行", signal: "trend_rebound", reason: "回踩 MA20",
    strategy: "trend_rebound", asof: "2026-09-24", price: "11.20", score: 0.62, rank_no: 3,
    risk_ex10_pp: "-0.21",
  });
  assert.equal(row.strategy, "trend_rebound");
  assert.equal(row.asof, "2026-09-24");
  assert.equal(row.price, 11.2);        // 字符串价格必须转成数值
  assert.equal(row.score, 0.62);
  assert.equal(row.rank, 3);            // 后端列名 rank_no → 前端 rank
  assert.equal(row.risk, -0.21);        // 后端列名 risk_ex10_pp → 前端 risk
});

test("风险分展示：缺值给占位符而不是 0，正负分别红绿", () => {
  assert.equal(riskText(null), "—");      // 闸门没判出来 ≠ 预测跑平全市场
  assert.equal(riskText(undefined), "—");
  assert.equal(riskText(NaN), "—");
  assert.equal(riskText(-0.213), "-0.21pp");
  assert.equal(riskText(0), "0.00pp");
  assert.equal(riskText(0.5), "+0.50pp");
  assert.equal(riskClass(-0.2), "down");
  assert.equal(riskClass(0.2), "up");
  assert.equal(riskClass(0), "up");
  assert.equal(riskClass(null), "");
});