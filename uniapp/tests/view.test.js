import test from "node:test";
import assert from "node:assert/strict";
import { toHoldingRow, toTradeRow, toPnlView, toSelectionJobRow, toSelectionResultRow,
         toMessageRow, toDiagRow, toPreviewRow, toUpgradeInfo, fmtTime,
         priceStampText, convictionText,
         riskText, riskClass, sortByRiskDesc, buyLevelText, sellTriggerText,
         orderPlanText, riskAdjustText, posteriorText } from "../src/logic/view.js";

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
              holding_pnl: -18050, pnl_pct: -10.7, diag_run_id: "r9",
              suggestion: "已跌破均线卖点 2.5%｜回本需 +12.0%",
              sell_trigger_price: 1512.3 };
  const row = toHoldingRow(s);
  assert.equal(row.cost, 1680.5);
  assert.equal(row.shares, 100);
  assert.equal(row.latestPrice, 1500);
  assert.equal(row.pnl, -18050);
  assert.equal(row.pnlPct, -10.7);
  assert.equal(row.action, "sell");
  assert.equal(row.diagRunId, "r9");
  assert.equal(row.sellTrigger, 1512.3);
  assert.equal(row.suggestion, "已跌破均线卖点 2.5%｜回本需 +12.0%");
});

test("可信度行：列表页剥掉卖点线（那是诊断当时的价算的，现价已换成实时），详情页原样", () => {
  const r = { suggestion: "均线卖点在下方 0.6%｜同类 hold 20 日内 45% 转为卖出｜回本需 +5.0%" };
  assert.equal(convictionText(r), "可信度 · 同类 hold 20 日内 45% 转为卖出｜回本需 +5.0%");
  assert.equal(convictionText(r, true),
               "可信度 · 均线卖点在下方 0.6%｜同类 hold 20 日内 45% 转为卖出｜回本需 +5.0%");
  // 只有一句卖点线时剥完就没什么可说的，别留个孤零零的前缀
  assert.equal(convictionText({ suggestion: "均线卖点在下方 0.6%" }), "");
  assert.equal(convictionText({ suggestion: "" }), "");
  assert.equal(convictionText({}), "");
});

test("持仓行：现价来源透传（老接口没有这两个字段时为空串）", () => {
  const row = toHoldingRow({ code: "1", quantity: 1, cost_price: 1,
                             price_time: "20261012161445", price_source: "live" });
  assert.equal(row.priceTime, "20261012161445");
  assert.equal(row.priceSource, "live");
  const legacy = toHoldingRow({ code: "1", quantity: 1, cost_price: 1 });
  assert.equal(legacy.priceTime, "");
  assert.equal(legacy.priceSource, "");
});

test("现价时刻文案：实时给时分，日线/快照给日期，让旧价一眼看得出来的", () => {
  assert.equal(priceStampText({ priceTime: "20261012161445", priceSource: "live" }), "16:14 实时");
  assert.equal(priceStampText({ priceTime: "2026-10-11", priceSource: "kline" }), "10-11 收盘");
  assert.equal(priceStampText({ priceTime: "2026-10-11", priceSource: "snapshot" }), "10-11 诊断");
  assert.equal(priceStampText({ priceTime: "", priceSource: "snapshot" }), "诊断快照");
  assert.equal(priceStampText({ priceTime: "", priceSource: "" }), "");
});

test("卖出触发价文案：未触发说\"动到哪开始卖\"，已触发说\"动回去就不必卖\"", () => {
  assert.equal(sellTriggerText({ action: "hold", latestPrice: 10, sellTrigger: 9.5 }),
               "跌破 9.50 触发卖出");
  assert.equal(sellTriggerText({ action: "add", latestPrice: 10, sellTrigger: 10.6 }),
               "涨破 10.60 触发卖出");
  assert.equal(sellTriggerText({ action: "sell", latestPrice: 9, sellTrigger: 9.5 }),
               "涨回 9.50 解除卖出信号");
  assert.equal(sellTriggerText({ action: "hold", latestPrice: 10, sellTrigger: 10 }),
               "触发线 10.00（价格已到位）");
  // 反算不出线（规则不含价格项、或行情缺失）时整行不给文案，不能显示 0.00
  assert.equal(sellTriggerText({ action: "hold", latestPrice: 10, sellTrigger: null }), "");
  assert.equal(sellTriggerText({ action: "hold", latestPrice: null, sellTrigger: 9.5 }), "");
  assert.equal(sellTriggerText({}), "");
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
    buyRef: null, buyInvalid: null,
    riskVerdict: "", riskReason: "", riskShares: null, riskAmount: null,
    riskFill: null, riskSlippageBp: null,
    // 判据后验：后端没给这三列时必须是空串/null（页面据此显示「未标定」）
    posteriorVerdict: "", posteriorExcess: null, posteriorN: null,
  });
});

test("选股结果行：插件富化字段落到 camelCase", () => {
  const row = toSelectionResultRow({
    code: "000001", name: "平安银行", signal: "trend_rebound", reason: "回踩 MA20",
    strategy: "trend_rebound", asof: "2026-09-24", price: "11.20", score: 0.62, rank_no: 3,
    risk_ex10_pp: "-0.21", buy_ref_price: "10.85", buy_invalid_price: 10.31,
  });
  assert.equal(row.strategy, "trend_rebound");
  assert.equal(row.asof, "2026-09-24");
  assert.equal(row.price, 11.2);        // 字符串价格必须转成数值
  assert.equal(row.score, 0.62);
  assert.equal(row.rank, 3);            // 后端列名 rank_no → 前端 rank
  assert.equal(row.risk, -0.21);        // 后端列名 risk_ex10_pp → 前端 risk
  assert.equal(row.buyRef, 10.85);
  assert.equal(row.buyInvalid, 10.31);
});

test("下单口径文案：风控给多少就说多少，缺字段就不提；没开风控的批次整行不显示", () => {
  assert.equal(orderPlanText({
    riskShares: 3800, riskAmount: 39140, riskFill: 10.31, riskSlippageBp: 12.5,
  }), "建议 3800 股 · 3.9 万元 · 预估成交 10.31 · 预估滑点 12.5bp");
  // 只给了股数：金额必须留空而不是编一个 0 万元
  assert.equal(orderPlanText({ riskShares: 3800 }), "建议 3800 股");
  assert.equal(orderPlanText({ riskFill: 10.3, riskSlippageBp: 8 }), "预估成交 10.30 · 预估滑点 8.0bp");
  assert.equal(orderPlanText({}), "");
  // 风控关闭时跑的旧结果：字段全空，不能显示成「建议 0 股」
  assert.equal(orderPlanText(toSelectionResultRow({ code: "1", signal: "x" })), "");
  assert.equal(riskAdjustText(toSelectionResultRow({ code: "1", signal: "x" })), "");
});

test("风控调整理由只在 adjust 时出现，allow 不给噪音", () => {
  assert.equal(riskAdjustText({ riskVerdict: "adjust", riskReason: "[position] 单票上限 4 万元" }),
               "[position] 单票上限 4 万元");
  assert.equal(riskAdjustText({ riskVerdict: "allow", riskReason: "" }), "");
  assert.equal(riskAdjustText({ riskVerdict: "allow", riskReason: "[position] 不该出现" }), "");
});

test("买点文案：判据里有几个位就说几个，都没有就整行不显示", () => {
  assert.equal(buyLevelText({ buyRef: 10.85, buyInvalid: 10.31 }), "买入参考 10.85 · 跌破 10.31 失效");
  // 低位反转只有失效位（买点就是信号收盘价），不能凭空补一个参考价
  assert.equal(buyLevelText({ buyRef: null, buyInvalid: 10.3 }), "跌破 10.30 失效");
  assert.equal(buyLevelText({ buyRef: 10.3, buyInvalid: null }), "买入参考 10.30");
  assert.equal(buyLevelText({ buyRef: null, buyInvalid: null }), "");
  assert.equal(buyLevelText({}), "");
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

test("依据文案：显示判据级 verdict，缺失时显式说未标定而不是整行消失", () => {
  assert.equal(posteriorText({ posteriorVerdict: "换时代/换抽样会翻号，未标定",
                               posteriorExcess: -0.32, posteriorN: 166120 }),
               "依据 · 换时代/换抽样会翻号，未标定 · 10日超额 -0.32pp · 样本 16.6 万点");
  // 正超额也要带符号，样本数不到 1 万就原样写
  assert.equal(posteriorText({ posteriorVerdict: "正超额·三轴同号",
                               posteriorExcess: 0.096, posteriorN: 8825 }),
               "依据 · 正超额·三轴同号 · 10日超额 +0.10pp · 样本 8825点");
  // 后验表还没建（后端给空串/None）：必须出现"未标定"，不能返回空串让这行消失——
  // 空行会被读成"没问题"，而真实含义是"还没测"。
  assert.equal(posteriorText({}), "依据 · 未标定（判据后验表还没建）");
  assert.equal(posteriorText({ posteriorVerdict: "  " }), "依据 · 未标定（判据后验表还没建）");
  assert.equal(posteriorText(undefined), "依据 · 未标定（判据后验表还没建）");
  // 只有 verdict、没有数字时不编造数字
  assert.equal(posteriorText({ posteriorVerdict: "分格样本不足（单格上涨概率区间>2pp），不定论" }),
               "依据 · 分格样本不足（单格上涨概率区间>2pp），不定论");

  // 映射层：后端列名 → camelCase，超额/样本数按数值取
  const row = toSelectionResultRow({ code: "1", signal: "s", posterior_verdict: "正超额·三轴同号",
                                     posterior_excess_pp: "1.20", posterior_n: "300" });
  assert.equal(row.posteriorVerdict, "正超额·三轴同号");
  assert.equal(row.posteriorExcess, 1.2);
  assert.equal(row.posteriorN, 300);
  assert.equal(posteriorText(row), "依据 · 正超额·三轴同号 · 10日超额 +1.20pp · 样本 300点");
});

test("展示顺序：按预期超额降序，没判分的排最后且互保名次序", () => {
  const rows = [
    { code: "A", rank: 1, risk: null },
    { code: "B", rank: 2, risk: -0.24 },
    { code: "C", rank: 3, risk: 5.46 },
    { code: "D", rank: 4, risk: undefined },
    { code: "E", rank: 5, risk: 0.05 },
  ];
  assert.deepEqual(sortByRiskDesc(rows).map((r) => r.code), ["C", "E", "B", "A", "D"]);
  assert.deepEqual(rows.map((r) => r.code), ["A", "B", "C", "D", "E"]);   // 不改原数组
  assert.equal(sortByRiskDesc([]).length, 0);
});
// —— 板块热力图：颜色档、文案与字段透传 ——
test("heatColor：红涨绿跌分档，缺值给中性灰而不是 0% 的平色", async () => {
  const { heatColor } = await import("../src/logic/view.js");
  assert.equal(heatColor(8).background, "#c62826");
  assert.equal(heatColor(4).background, "#e05044");
  assert.equal(heatColor(0.5).background, "#f7c7b8");
  assert.equal(heatColor(0).background, "#e9edf5");
  assert.equal(heatColor(-1).background, "#b9e2c8");
  assert.equal(heatColor(-2).background, "#6fc79a");
  assert.equal(heatColor(-8).background, "#12885a");
  assert.equal(heatColor(null).background, "#f0f2f7");
  assert.equal(heatColor(undefined).color, "#a2a9b5");
});

test("signedPct / yiText：正负号与亿万元量纲", async () => {
  const { signedPct, yiText } = await import("../src/logic/view.js");
  assert.equal(signedPct(3.2145), "+3.21%");
  assert.equal(signedPct(-1.5), "-1.50%");
  assert.equal(signedPct(null), "—");
  assert.equal(yiText(2.5e8), "2.50亿");
  assert.equal(yiText(3.4e5), "34万");
  assert.equal(yiText(0), "0");
  assert.equal(yiText(null), "—");
});

test("板块格子行：字段透传，前端不加总不重算", async () => {
  const { toBoardTile } = await import("../src/logic/view.js");
  const t = toBoardTile({
    kind: "industry", board_code: "A05", board_name: "农林牧渔", ret_pct: 1.5,
    stock_count: 7, member_count: 9, up_count: 7, down_count: 0, flat_count: 0,
    limit_up_count: 3, turnover: 1.2e9, turnover_ratio: 1.58,
    leader_name: "宏英智能", leader_ret: 11.86, lagger_name: "x", lagger_ret: -1,
  });
  assert.deepEqual([t.code, t.name, t.retText], ["A05", "农林牧渔", "+1.50%"]);
  assert.deepEqual([t.stockCount, t.memberCount, t.limitUpCount], [7, 9, 3]);
  assert.equal(t.heat.background, "#ef8371");
  assert.equal(t.turnoverRatio, 1.58);
});

test("成分股行：停牌标记所需的 days/end_date 要带上", async () => {
  const { toSectorMemberRow } = await import("../src/logic/view.js");
  const m = toSectorMemberRow({ stock_code: "605303", stock_name: "园林股份", close: 32.21,
    ret_pct: 23.6468, pct_last: 2.156, days: 3, limit_up: true, turnover: 8.6e8, end_date: "2026-10-09" });
  assert.deepEqual([m.code, m.name, m.retText], ["605303", "园林股份", "+23.65%"]);
  assert.equal(m.limitUp, true);
  assert.equal(m.days, 3);
  assert.equal(m.endDate, "2026-10-09");
});
