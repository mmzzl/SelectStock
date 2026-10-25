import test from "node:test";
import assert from "node:assert/strict";
import { toHoldingRow, toTradeRow, toPnlView, toSelectionJobRow, toSelectionResultRow,
         toMessageRow, toDiagRow, toPreviewRow, toUpgradeInfo, fmtTime,
         priceStampText, convictionText, convictionHead, convictionLevelText, convictionLevelClass,
         convictionNote, hasConviction, takeProfitText, gameText,
         riskText, riskClass, isNa, sortByRiskDesc, buyLevelText, sellTriggerText,
         orderPlanText, riskAdjustText, posteriorText, gateText, dayRankText,
         scoreText, toStrategyOption } from "../src/logic/view.js";

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

test("持仓行：结构化可信度/止盈/博弈字段透传；字段整段缺失时退化为 null/空串", () => {
  const row = toHoldingRow({
    code: "600519", name: "贵州茅台", quantity: 100, cost_price: 1680.5,
    latest_price: 1500, sell_trigger_price: 1512.3,
    suggestion: "已跌破均线卖点 2.5%｜回本需 +12.0%",
    conviction_pct: "62.5", conviction_level: "高",
    conviction_text: "同类 hold 20 日内 45% 转为卖出",
    take_profit_price: "1720", game_v: 0.62, game_s: "0.38", game_text: "对手盘偏多",
  });
  assert.equal(row.convictionPct, 62.5);   // 字符串也要转成数值
  assert.equal(row.convictionLevel, "高");
  assert.equal(row.convictionText, "同类 hold 20 日内 45% 转为卖出");
  assert.equal(row.takeProfit, 1720);
  assert.equal(row.gameV, 0.62);
  assert.equal(row.gameS, 0.38);
  assert.equal(row.gameText, "对手盘偏多");

  // 老接口没有这批字段：全部退化成 null/空串，页面据此走 suggestion 兜底
  const legacy = toHoldingRow({ code: "1", quantity: 1, cost_price: 1 });
  assert.equal(legacy.convictionPct, null);
  assert.equal(legacy.convictionLevel, "");
  assert.equal(legacy.convictionText, "");
  assert.equal(legacy.takeProfit, null);
  assert.equal(legacy.gameV, null);
  assert.equal(legacy.gameS, null);
  assert.equal(legacy.gameText, "");
});

test("可信度结构化：百分比取整 + 等级徽章 + 短说明；没有百分比就退回老 suggestion 文案", () => {
  const r = { convictionPct: 62.5, convictionLevel: "高",
              convictionText: "同类 hold 20 日内 45% 转为卖出" };
  assert.equal(convictionHead(r), "可信度 63%");   // 压到整数
  assert.equal(convictionLevelText(r), "高");
  assert.equal(convictionLevelClass(r), "high");
  assert.equal(convictionNote(r), "同类 hold 20 日内 45% 转为卖出");
  assert.equal(convictionText(r), "可信度 63% · 同类 hold 20 日内 45% 转为卖出");

  // 只有百分比、没有说明时不留尾巴
  assert.equal(convictionText({ convictionPct: 30 }), "可信度 30%");
  assert.equal(convictionNote({ convictionPct: 30 }), "");

  // 等级三档各自映射到样式类；给了不认识的等级就不出徽章
  assert.equal(convictionLevelClass({ convictionLevel: "中" }), "mid");
  assert.equal(convictionLevelClass({ convictionLevel: "低" }), "low");
  assert.equal(convictionLevelText({ convictionLevel: "极高" }), "");
  assert.equal(convictionLevelClass({ convictionLevel: "极高" }), "");

  // 百分比缺失（缺失工件）→ 主行退回老文案，说明不单独冒出来免得重复
  const legacy = { suggestion: "均线卖点在下方 0.6%｜同类 hold 20 日内 45% 转为卖出" };
  assert.equal(convictionHead(legacy), "可信度 · 同类 hold 20 日内 45% 转为卖出");
  assert.equal(convictionNote(legacy), "");
  assert.equal(convictionText(legacy), "可信度 · 同类 hold 20 日内 45% 转为卖出");
  // 详情页 withLine 保留首段（价格是那一次诊断的快照，口径自洽）
  assert.equal(convictionHead(legacy, true),
               "可信度 · 均线卖点在下方 0.6%｜同类 hold 20 日内 45% 转为卖出");
  // 百分比是坏数（NaN）也当没有，不能印「可信度 NaN%」
  assert.equal(convictionHead({ convictionPct: "abc", suggestion: "x｜y" }), "可信度 · y");
});

test("止盈价格线：跟止损同口径，目标或现价缺一个就整行不显示", () => {
  assert.equal(takeProfitText({ latestPrice: 10, takeProfit: 12.8 }), "涨到 12.80");
  assert.equal(takeProfitText({ latestPrice: 13, takeProfit: 12.8 }), "回落到 12.80");
  assert.equal(takeProfitText({ latestPrice: 12.8, takeProfit: 12.8 }), "12.80（价格已到位）");
  assert.equal(takeProfitText({ latestPrice: 10, takeProfit: null }), "");
  assert.equal(takeProfitText({ latestPrice: null, takeProfit: 12.8 }), "");
  assert.equal(takeProfitText({}), "");
});

test("博弈参考行：V/S 标参考值、game_text 标定性，全缺就整行不显示", () => {
  assert.equal(gameText({ gameV: 0.62, gameS: 0.38, gameText: "对手盘偏多" }),
               "博弈参考 · V 0.62 · S 0.38（参考值） · 定性：对手盘偏多");
  assert.equal(gameText({ gameV: 0.62 }), "博弈参考 · V 0.62（参考值）");
  assert.equal(gameText({ gameText: "多空胶着" }), "博弈参考 · 定性：多空胶着");
  assert.equal(gameText({}), "");
  assert.equal(gameText({ gameV: null, gameS: null, gameText: "" }), "");
});

test("可信度块存在性：任一子行有内容才出块，全空不出", () => {
  assert.equal(hasConviction({ convictionPct: 50 }), true);
  assert.equal(hasConviction({ convictionLevel: "中" }), true);
  assert.equal(hasConviction({ action: "hold", latestPrice: 10, sellTrigger: 9.5 }), true);
  assert.equal(hasConviction({ latestPrice: 10, takeProfit: 12 }), true);
  assert.equal(hasConviction({ gameText: "x" }), true);
  assert.equal(hasConviction({}), false);
  assert.equal(hasConviction({ suggestion: "" }), false);
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
    // adx14 闸门留痕：闸门没开时是 null/空串（页面整句不显示，不能印「分位 0」）
    adx14Pct: null, adx14Gate: "", naFields: [],
  });
});

test("adx14 闸门留痕：shadow 期的放行/剔剔除都看得见", () => {
  const row = toSelectionResultRow({ code: "000001", name: "平安银行", signal: "pattern_reversal",
                                     reason: "低位V型反转", adx14_pct: 97.53, adx14_gate: "pass" });
  assert.equal(row.adx14Pct, 97.53);
  assert.equal(row.adx14Gate, "pass");
  assert.equal(gateText(row), "adx14 闸门 · 放行（分位 98，shadow 期只记录不拦）");
  // 分位取不到时要明说「未知」，不能落成 0（0 分位是"当日全市场最弱"这个事实）
  assert.equal(gateText({ adx14Gate: "reject" }), "adx14 闸门 · 剔除（分位未知，shadow 期只记录不拦）");
  assert.equal(gateText({}), "");
  assert.equal(gateText({ adx14Gate: "  " }), "");
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
  // 引擎声明不算买点（qlib 没有均线/箱沿这类判据）时这行必须说话，
  // 因为「没有失效位」看着像"这笔买入不会错"，实际是"没人给你算过"。
  // 措辞不能写「该引擎不出价」：同张卡片上方就显示着基准价（信号日收盘），
  // 那句话自相矛盾；缺的是**判据价位**，不是价格本身。
  assert.equal(buyLevelText({ buyRef: null, buyInvalid: null, naFields: ["buy_ref_price"] }),
               "买点参考位/失效位 · 不适用（该引擎只出排序分，不给价位；能对照的只有上方基准价）");
  // 有数字时不适用让位给数字（同一引擎不会既不算又算出来）
  assert.equal(buyLevelText({ buyRef: 10.3, naFields: ["buy_ref_price"] }), "买入参考 10.30");
});

test("口径段：选股页不得声称引擎「不出价」，要写实测过的价位止损结论", async () => {
  const { readFileSync } = await import("node:fs");
  const page = readFileSync(new URL("../src/pages/selection/index.vue", import.meta.url), "utf8");
  assert.ok(!page.includes("不出价位"), "页头口径段还在说「该引擎不出价位」——基准价就在卡片上，这句话自相矛盾");
  assert.ok(page.includes("价位止损") && page.includes("少赚"),
            "口径段要交代价位止损在这份名单上实测过（设了反而扣分）");
  assert.ok(page.includes("换出纪律是名次"), "口径段要给出真正的换出口径，不然读者只剩「不适用」三个字");
});

test("行解析：na_fields 逗号串拆成数组，缺列拆成空数组", () => {
  const row = toSelectionResultRow({ code: "600000", na_fields: "risk_ex10_pp,buy_ref_price" });
  assert.deepEqual(row.naFields, ["risk_ex10_pp", "buy_ref_price"]);
  assert.deepEqual(toSelectionResultRow({ code: "600000" }).naFields, []);
  assert.equal(isNa(row, "risk_ex10_pp"), true);
  assert.equal(isNa(row, "adx14_pct"), false);
});

test("风险分展示：缺值给占位符而不是 0，正负分别红绿", () => {
  assert.equal(riskText(null), "—");      // 闸门没判出来 ≠ 预测跑平全市场
  assert.equal(riskText(undefined), "—");
  assert.equal(riskText(NaN), "—");
  // 「引擎不算」和「算了没判出来」是两句话：前者必须写不适用，后者才用「—」
  assert.equal(riskText(null, true), "不适用");
  assert.equal(riskClass(null, true), "");
  assert.equal(riskText(0.5, true), "+0.50pp");   // 有数就用数，声明不该盖掉真值
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
  // qlib 引擎根本没有「判据」这个东西，沿用上句等于给了一句解释不成立的话：
  // 后验表是建好了的，只是这个引擎不产 verdict。
  assert.equal(posteriorText({ naFields: ["posterior_verdict"] }),
               "依据 · 不适用（该引擎只出排序分，没有判据后验）");
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

test("展示顺序：模型榜按护栏后名次排在最前，旧 hot_pref 的 #1 不能抢第一行", () => {
  const rows = [
    { code: "H1", strategy: "hot_pref", rank: 1, risk: 9 },
    { code: "Q2", strategy: "qlib_alpha158", rank: 2, risk: null },
    { code: "Q1", strategy: "qlib_alpha158", rank: 1, risk: null },
  ];
  assert.deepEqual(sortByRiskDesc(rows).map((r) => r.code), ["Q1", "Q2", "H1"]);
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

test("消息面行：方向标签与强度口径由前端固定，个股/板块要分得开", async () => {
  const { toNewsItem, NEWS_DIRS } = await import("../src/logic/view.js");
  assert.deepEqual(NEWS_DIRS.map((d) => d.key), ["bearish", "risk", "bullish", "watch"]);

  const bear = toNewsItem({ code: "000869", name: "张裕A", category: "个股", direction: "bearish",
    score: -8, reason: "控股股东减持", news_count: 3, holding: true });
  assert.deepEqual([bear.dirText, bear.dirCls, bear.scoreText], ["利空", "neg", "-8"]);
  assert.equal(bear.isStock, true);
  assert.equal(bear.holding, true);
  assert.equal(bear.newsCount, 3);

  // 板块码不是六位数字：不能当成个股跳 K 线
  const board = toNewsItem({ code: "BK0438", name: "电池", direction: "bearish", score: -6 });
  assert.equal(board.isStock, false);
  assert.equal(board.newsCount, 0);

  // 关注没有分数，显示优先级；未知方向不能崩
  const w = toNewsItem({ code: "", name: "捷荣技术", direction: "watch", priority: "高", score: 0 });
  assert.deepEqual([w.dirText, w.scoreText], ["关注", "高"]);
  assert.equal(toNewsItem({ direction: "奇怪", name: "" }).name, "—");
});

test("消息面健康条：缺批次/失败率高/旧批次都要出声，健康时留空", async () => {
  const { newsBanner } = await import("../src/logic/view.js");
  assert.equal(newsBanner({}).cls, "bad");
  assert.match(newsBanner({ day: "" }).text, /近 5 天/);

  const failing = newsBanner({ day: "2026-10-13", stale_days: 0,
    health: { analyzed: 167, failed: 120, fail_rate: 0.72 } });
  assert.equal(failing.cls, "bad");
  assert.match(failing.text, /120\/167/);

  const stale = newsBanner({ day: "2026-10-11", stale_days: 2,
    health: { analyzed: 167, failed: 10, fail_rate: 0.06 } });
  assert.equal(stale.cls, "muted");
  assert.match(stale.text, /2 天前/);

  assert.deepEqual(newsBanner({ day: "2026-10-13", stale_days: 0,
    health: { analyzed: 167, failed: 0, fail_rate: 0 } }), { text: "", cls: "" });
});

test("消息面页面在路由白名单里（否则进页面会被弹回持仓）", async () => {
  const { resolveRoute } = await import("../src/logic/guard.js");
  assert.deepEqual(resolveRoute({ path: "pages/news/index", loggedIn: true }), { ok: true });
  assert.deepEqual(resolveRoute({ path: "pages/news/index", loggedIn: false }),
    { redirect: "pages/login/index" });
});

test("风险条目按读取层整形后的样子渲染：标题一行、没有重复正文", async () => {
  const { toNewsItem } = await import("../src/logic/view.js");
  // 整形在 stock_selector/news/read.py 做（一处，App 与推送共用），前端只透传
  const r = toNewsItem({ code: "", name: "IPO欺诈调查可能导致茶百道上市失败或严重推迟",
    category: "", direction: "risk", score: -8, reason: "" });
  assert.deepEqual([r.name, r.reason, r.category, r.scoreText],
    ["IPO欺诈调查可能导致茶百道上市失败或严重推迟", "", "", "-8"]);
});

test("当日榜内名次：有名次就说名次，拿不到就说原因，绝不补一个猜的名次", () => {
  const meta = { supported: true, date: "2026-10-19", pool_size: 300, universe: 299, exit_rank: 100 };
  // 榜内 + 还在卖出线内
  assert.equal(dayRankText({ code: "600000", in_pool: true, rank: 7, universe: 299 }, meta),
    "榜内名次 · 第 7 名 / 共 299 只 · 还在卖出线（前 100 名）内");
  // 榜内但已跌出卖出线（卖出线是回测 band 的那条，不是页面自己写的数）
  assert.equal(dayRankText({ code: "600000", in_pool: true, rank: 138, universe: 299 }, meta),
    "榜内名次 · 第 138 名 / 共 299 只 · 已跌出卖出线（前 100 名）");
  // 池外的持仓：模型从没给这只打过分，说清楚它不是"排在池子后面"
  assert.equal(dayRankText({ code: "510300", in_pool: false, rank: null, universe: 299 }, meta),
    "榜内名次 · 不在当前 300 只候选池里，模型没给这只打过分");
  // 在池里但当日没行情 ⇒ 不能说"跌出榜外"，那是会让人卖票的一句话
  assert.equal(dayRankText({ code: "600001", in_pool: true, rank: null, universe: 299 }, meta),
    "榜内名次 · 在候选池里，但当日没有可打分的行情（停牌或日线断档）");
  // 名次是列表之后异步补的：还没到就整行不显示（页头会写失败原因，这里不编）
  assert.equal(dayRankText(undefined, meta), "");
  assert.equal(dayRankText({ code: "600000", in_pool: true, rank: 3 }, null), "");
  // 字符串名次（接口给的可能是 "7"）一样能用；缺 universe/exit_rank 就少说一截，不编
  assert.equal(dayRankText({ code: "600000", in_pool: true, rank: "3", universe: 299 },
    { supported: true, pool_size: 300, universe: 0, exit_rank: null }),
    "榜内名次 · 第 3 名");
});

test("排序分展示成百分制：0~1 的分位乘成 0~100，空值给「—」", () => {
  // 引擎落库的是「当日票池里的位置」(0~1，第一名正好 1.000)。使用者要的是满分 100，
  // 所以乘法放在展示层做一次；口径不变、只换刻度。
  assert.equal(scoreText({ score: 1 }), "100");
  assert.equal(scoreText({ score: 0.997 }), "99.7");
  assert.equal(scoreText({ score: 0.8732 }), "87.3");
  assert.equal(scoreText({ score: 0 }), "0");
  assert.equal(scoreText({ score: null }), "—");
  assert.equal(scoreText({}), "—");
  // 越界的脏数据不能显示成 120 分
  assert.equal(scoreText({ score: 1.2 }), "100");
});

test("策略选项：插件给了 label 就用它，没给才从 description 抠前缀", () => {
  assert.equal(toStrategyOption({ name: "hot_pref", label: "热门偏好榜",
                                  description: "热门偏好榜：四条判据" }).label, "热门偏好榜");
  assert.equal(toStrategyOption({ name: "x", description: "自研内核：三条规则" }).label, "自研内核");
  assert.equal(toStrategyOption({ name: "x" }).label, "x");
});

test("口径段：选股页要说清「满分 100」和两条榜各是什么", async () => {
  const { readFileSync } = await import("node:fs");
  const page = readFileSync(new URL("../src/pages/selection/index.vue", import.meta.url), "utf8");
  assert.ok(page.includes("scoreText"), "排序分那一格必须走 scoreText，不能再拿 fixed3 直接印 0~1");
  assert.ok(page.includes("满分 100"), "要写清分数是百分制刻度");
  assert.ok(page.includes("热门偏好榜"), "第二条榜是什么得在页面上说一句");
  assert.ok(page.includes("不能当买入理由") || page.includes("不是把握"), "分高≠把握大这句话不能丢");
  // 两条榜可以各含同一只票，:key 只用代码会让 Vue 拿到重复键
  assert.ok(/:key="[^"]*strategy[^"]*code[^"]*"/.test(page),
            "结果卡的 :key 要带上策略名，否则同一只票进两条榜时 Vue 键冲突");
});
