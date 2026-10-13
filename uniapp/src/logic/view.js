const num = (v) => (v === undefined || v === null ? null : Number(v));
const ACTION_TEXT = { sell: "建议卖出", hold: "持有", add: "建议加仓" };
const DIR_TEXT = { buy: "买入", sell: "卖出" };

function parseStrList(raw) {
  if (Array.isArray(raw)) return raw.map(String);
  if (typeof raw !== "string") return [];
  try { const v = JSON.parse(raw); return Array.isArray(v) ? v : []; } catch { return []; }
}

// 后端时间戳是 naive UTC（datetime.utcnow），直接展示会差 8 小时且带微秒尾巴；
// 这里统一转本地时间并压到分钟精度。已带时区的字符串按原样解析。
export function fmtTime(raw) {
  if (!raw) return "";
  const s = String(raw).trim().replace(" ", "T");
  const t = Date.parse(/[zZ]$|[+-]\d{2}:?\d{2}$/.test(s) ? s : s + "Z");
  if (Number.isNaN(t)) return String(raw);
  const d = new Date(t);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function toHoldingRow(s) {
  return {
    code: s.code,
    name: s.name || s.code,
    // 持仓行用 cost_price/quantity，诊断明细同样是 quantity（后端无 cost/shares 字段）
    cost: num(s.cost_price), shares: num(s.quantity),
    action: s.action || null,
    actionText: ACTION_TEXT[s.action] || "无建议",
    actionReason: s.action_reason || "",
    suggestion: s.suggestion || "",
    latestPrice: num(s.latest_price),
    priceTime: s.price_time || "", priceSource: s.price_source || "",
    pnl: num(s.holding_pnl), pnlPct: num(s.pnl_pct),
    sellTrigger: num(s.sell_trigger_price),
    diagRunId: s.diag_run_id || null,
  };
}

/** 现价是哪一刻的数：实时价给时分，退回日线/诊断快照时给日期。
 *  持仓价曾经整天不动（读的是上一交易日收盘），光看数字看不出它过期了，所以来源必须写出来。 */
export function priceStampText(r) {
  const stamp = String(r?.priceTime || "");
  const src = r?.priceSource || "";
  if (!stamp && !src) return "";
  if (src === "live") {
    // 腾讯行情时间是 20261012161445，页面只要时分
    return /^\d{14}$/.test(stamp) ? `${stamp.slice(8, 10)}:${stamp.slice(10, 12)} 实时` : "实时";
  }
  const day = stamp.slice(5, 10);          // 日线/快照的时间戳就是一日期 YYYY-MM-DD
  if (src === "snapshot") return day ? `${day} 诊断` : "诊断快照";
  return day ? `${day} 收盘` : "";
}

/** 卖出价格线的文案。线是插件按同一条 sell 规则反算的翻转价，所以它和 action 不会打架：
 *  没触发时是「动到哪就开始卖」，已触发时是「动回去就不必卖」。 */
export function sellTriggerText(r) {
  const trig = num(r?.sellTrigger);
  const latest = num(r?.latestPrice);
  if (trig === null || latest === null || latest <= 0) return "";
  const p = trig.toFixed(2);
  if (Math.abs(trig - latest) < 0.005) return `触发线 ${p}（价格已到位）`;
  const down = trig < latest;
  if (r.action === "sell") return `${down ? "跌破" : "涨回"} ${p} 解除卖出信号`;
  return `${down ? "跌破" : "涨破"} ${p} 触发卖出`;
}

/** 判定可信度：插件给的是「处在同一状态的持仓点整体后来怎么样」的同类频率，不是这只票的预测。
 *  串的第一段永远是卖点线差价，而它是拿**诊断当时**的价算的；持仓页的现价已经换成实时价，
 *  留着会出现「说在下方、其实已跌破」的反话，所以列表页剥掉第一段（触发价另有 sellTriggerText 现算）。
 *  诊断详情页整行都在讲那一次快照，价格口径自洽，传 withLine=true 原样给。 */
export function convictionText(r, withLine = false) {
  const bits = String(r?.suggestion || "").split("｜").map((s) => s.trim()).filter(Boolean);
  if (!bits.length) return "";
  if (!withLine) bits.shift();
  return bits.length ? `可信度 · ${bits.join("｜")}` : "";
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
  return {
    code: s.code, name: s.name || s.code, signal: s.signal || "", reason: s.reason || "",
    // 选股插件 P1 富化字段；后端列名 rank_no（rank 是 SQL 保留字），这里统一回 rank
    strategy: s.strategy || s.signal || "", asof: s.asof || "",
    price: num(s.price), score: num(s.score), rank: num(s.rank ?? s.rank_no),
    // 买入风险分（百分点）：预测 10 日同日基线超额，只有过了风险闸门的票才有值
    risk: num(s.risk_ex10_pp),
    // 买点参考位/失效位：策略判据里算出的均线、箱体上沿、6 日低点（不是预测价）
    buyRef: num(s.buy_ref_price), buyInvalid: num(s.buy_invalid_price),
    // 风控后的最终决策：allow=信号原样可下单，adjust=能下单但下单量被规则压过。
    // 被拦掉的票内核不会返回，所以这里不会出现 "block"；字段全空表示这批结果跑在风控关闭时。
    riskVerdict: s.risk_verdict || "", riskReason: s.risk_reason || "",
    riskShares: num(s.risk_shares), riskAmount: num(s.risk_amount),
    riskFill: num(s.risk_fill_price), riskSlippageBp: num(s.risk_slippage_bp),
    // 判据级历史后验（这条判据本身值多少钱，不是这只票的评分）。
    // verdict 已经由内核把「样本够不够」和「换时代/换抽样同不同号」合成一句话，
    // 页面只显示 verdict，绝不单独显示 excess —— 那会把翻号的抽样噪声说成结论。
    posteriorVerdict: s.posterior_verdict || "",
    posteriorExcess: num(s.posterior_excess_pp), posteriorN: num(s.posterior_n),
    adx14Pct: num(s.adx14_pct), adx14Gate: s.adx14_gate || "",
  };
}

/** 依据一行话（与诊断页 `可信度 · …` 同一措辞风格）。后验表还没建时必须显式说「未标定」，
 *  不能整行消失——用户看到"没有这一行"会理解成"没问题"，而实际是"还没测"。 */
export function posteriorText(r) {
  const verdict = (r?.posteriorVerdict || "").trim();
  if (!verdict) return "依据 · 未标定（判据后验表还没建）";
  const parts = [`依据 · ${verdict}`];
  const excess = num(r?.posteriorExcess);
  if (excess !== null) parts.push(`10日超额 ${excess > 0 ? "+" : ""}${excess.toFixed(2)}pp`);
  const n = num(r?.posteriorN);
  if (n !== null) parts.push(`样本 ${n >= 10000 ? `${(n / 10000).toFixed(1)} 万` : n}点`);
  return parts.join(" · ");
}

/** adx14 闸门在 shadow 期的留痕文案：分位 + 放行/剔除。闸门没开就整句不显示。 */
export function gateText(r) {
  const gate = (r?.adx14Gate || "").trim();
  const pct = num(r?.adx14Pct);
  if (!gate) return "";
  const where = pct === null ? "分位未知" : `分位 ${pct.toFixed(0)}`;
  return `adx14 闸门 · ${gate === "pass" ? "放行" : "剔除"}（${where}，shadow 期只记录不拦）`;
}

/** 风控给元 → 显示用万元：只是单位换算，数字本身是内核算好的，前端不做业务计算。 */
function wanText(v) {
  return `${(v / 10000).toFixed(1)} 万元`;
}

/** 下单口径一行话：建议买多少股、预估什么价成交、预估滑点多少。缺哪个不说哪个。 */
export function orderPlanText(r) {
  const parts = [];
  const shares = num(r?.riskShares);
  if (shares !== null) {
    const amount = num(r?.riskAmount);
    parts.push(`建议 ${shares} 股${amount !== null ? ` · ${wanText(amount)}` : ""}`);
  }
  const fill = num(r?.riskFill);
  if (fill !== null) parts.push(`预估成交 ${fill.toFixed(2)}`);
  const bp = num(r?.riskSlippageBp);
  if (bp !== null) parts.push(`预估滑点 ${bp.toFixed(1)}bp`);
  return parts.join(" · ");
}

/** 风控改过下单量时把内核的中文理由原样说给用户；原样放行时不给噪音。 */
export function riskAdjustText(r) {
  return r?.riskVerdict === "adjust" ? (r.riskReason || "") : "";
}

/** 买点文案：两个位都来自策略自己的判据，缺哪个就不说哪个；都没有就整行不显示。 */
export function buyLevelText(r) {
  const ref = num(r?.buyRef);
  const stop = num(r?.buyInvalid);
  const parts = [];
  if (ref !== null) parts.push(`买入参考 ${ref.toFixed(2)}`);
  if (stop !== null) parts.push(`跌破 ${stop.toFixed(2)} 失效`);
  return parts.join(" · ");
}

/** 买入风险分的展示文本（百分点，带符号）。没值给占位符——0 是"预测跑平当日全市场"，
 *  和"闸门没判出来"是两回事，不能拿 0 当缺省。 */
export function riskText(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  const n = Number(v);
  return `${n > 0 ? "+" : ""}${n.toFixed(2)}pp`;
}

/** 红涨绿跌（A 股习惯）：预测超额为正给 .up，为负给 .down，缺值不给颜色。 */
export function riskClass(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "";
  return Number(v) >= 0 ? "up" : "down";
}

/** 结果页展示顺序：按「预期 10 日超额」从高到低，没有风险分的排最后。
 *
 *  不用后端的 rank：box_oscillate 的强度分会饱和到 1.000，同分再按代码字符串兜底，
 *  头部名次并不携带任何信息（2026-10-11 实测榜首是货币 ETF）。缺值排最后而不是排 0，
 *  因为"闸门没判出来"和"预测跑平大盘"是两回事。
 */
export function sortByRiskDesc(rows) {
  const value = (r) => {
    const v = r?.risk;
    if (v === null || v === undefined || Number.isNaN(Number(v))) return -Infinity;
    return Number(v);
  };
  // 两个都没判分时 y-x 会是 NaN（规范按 0 处理，但不依赖实现），显式返回 0 让它们保序
  return [...rows].sort((a, b) => (value(a) === value(b) ? 0 : value(b) - value(a)));
}

/** 插件 /strategy-detail 的一行 → 前端选项。中文标签只来自 description，不硬编码策略名。 */
export function toStrategyOption(d) {
  return {
    name: d.name || "", label: (d.description || "").split("：")[0] || d.name || "",
    desc: d.description || "", requiredRows: num(d.required_rows), asOf: Boolean(d.as_of),
  };
}

export function toMessageRow(s) {
  return {
    id: s.id, eventCode: s.event_code || "", title: s.title || "", content: s.content || "",
    unread: Boolean(s.read_status),   // 后端 read_status: True=未读（read_at is None）
    createdAt: s.created_at || null,
  };
}

export function toDiagRow(s) {
  return {
    runId: s.run_id, day: s.natural_day, status: s.status,
    holdingCount: num(s.held_code_count), actionCount: num(s.action_count),
    totalPnl: num(s.total_pnl), updatedAt: s.updated_at || "",
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
// —— 板块热力图（格子颜色 = 区间涨幅，红涨绿跌；其余指标只在详情页出文字）——
const HEAT_STOPS = [
  { min: 6, bg: "#c62826", fg: "#ffffff" },
  { min: 3, bg: "#e05044", fg: "#ffffff" },
  { min: 1.5, bg: "#ef8371", fg: "#ffffff" },
  { min: 0.005, bg: "#f7c7b8", fg: "#7a2b21" },
  { min: -0.005, bg: "#e9edf5", fg: "#5b6270" },
  { min: -1.5, bg: "#b9e2c8", fg: "#1c5a35" },
  { min: -3, bg: "#6fc79a", fg: "#0d3f24" },
  { min: -6, bg: "#2fa26e", fg: "#ffffff" },
  { min: -Infinity, bg: "#12885a", fg: "#ffffff" },
];

/** 格子底色：涨幅缺失（数据没跑出来）给中性灰，绝不给「0% 的平色」骗人。 */
export function heatColor(ret) {
  const v = num(ret);
  if (v === null) return { background: "#f0f2f7", color: "#a2a9b5" };
  const stop = HEAT_STOPS.find((s) => v >= s.min) || HEAT_STOPS[HEAT_STOPS.length - 1];
  return { background: stop.bg, color: stop.fg };
}

export function signedPct(v, digits = 2) {
  const n = num(v);
  if (n === null) return "—";
  return `${n > 0 ? "+" : ""}${n.toFixed(digits)}%`;
}

/** 成交额单位是元，页面按亿/万显示；后端已汇总好，这里只做量纲换词。 */
export function yiText(v) {
  const n = num(v);
  if (n === null) return "—";
  const a = Math.abs(n);
  if (a >= 1e8) return `${(n / 1e8).toFixed(2)}亿`;
  if (a >= 1e4) return `${(n / 1e4).toFixed(0)}万`;
  return n.toFixed(0);
}

export function toBoardTile(b) {
  return {
    code: b.board_code,
    kind: b.kind,
    name: b.board_name || b.board_code,
    ret: num(b.ret_pct),
    retText: signedPct(b.ret_pct),
    heat: heatColor(b.ret_pct),
    stockCount: num(b.stock_count),
    memberCount: num(b.member_count),
    upCount: num(b.up_count),
    downCount: num(b.down_count),
    limitUpCount: num(b.limit_up_count),
    turnover: num(b.turnover),
    turnoverRatio: num(b.turnover_ratio),
    leaderName: b.leader_name || "",
    leaderRet: num(b.leader_ret),
    laggerName: b.lagger_name || "",
    laggerRet: num(b.lagger_ret),
  };
}

export function toSectorMemberRow(m) {
  return {
    code: m.stock_code,
    name: m.stock_name || m.stock_code,
    close: num(m.close),
    ret: num(m.ret_pct),
    retText: signedPct(m.ret_pct),
    pctLast: num(m.pct_last),
    turnover: num(m.turnover),
    days: num(m.days),
    limitUp: m.limit_up === true,
    endDate: m.end_date || "",
  };
}

/** K 线条目：canvas 要的字段全在这里对齐好，缺最高/最低时退化成用收盘画。 */
export function toKlineBar(b) {
  return {
    date: b.date,
    open: num(b.open), high: num(b.high ?? b.close), low: num(b.low ?? b.close),
    close: num(b.close), volume: num(b.volume),
    ma5: num(b.ma5), ma10: num(b.ma10), ma20: num(b.ma20), ma60: num(b.ma60),
  };
}
