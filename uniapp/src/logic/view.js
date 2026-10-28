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
    // 结构化可信度（新接口）：整段缺失时全为 null/空串，页面退回下面的 suggestion 文案
    convictionPct: num(s.conviction_pct),
    convictionLevel: s.conviction_level || "",
    convictionText: s.conviction_text || "",
    // 价格线：止损沿用 sell_trigger_price，止盈是新字段
    takeProfit: num(s.take_profit_price),
    // 博弈参考：V/S 是参考数值、game_text 是定性描述，页面压到次要一行
    gameV: num(s.game_v), gameS: num(s.game_s), gameText: s.game_text || "",
    // 筹码三档价位 + 口径句（名单页同一套函数渲染）
    buyRef: num(s.buy_ref_price), sellTarget: num(s.sell_target_price),
    buyInvalid: num(s.buy_invalid_price), levelNote: s.level_note || "",
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

// 可信度等级徽章：接口只给「高/中/低」三档，颜色沿用现有语义（绿=高、琥珀=中、灰=低）
const CONVICTION_LEVELS = { "高": "high", "中": "mid", "低": "low" };

/** 结构化可信度百分比：接口给 0-100 的浮点，页面压到整数；NaN/缺值都当作没有。 */
function convictionPct(r) {
  const pct = num(r?.convictionPct);
  return pct !== null && Number.isFinite(pct) ? pct : null;
}

/** 旧口径（结构化字段缺失时的兜底）：suggestion 用「｜」分段，插件给的是「处在同一状态的
 *  持仓点整体后来怎么样」的同类频率，不是这只票的预测。串的第一段永远是卖点线差价，而它是拿
 *  **诊断当时**的价算的；持仓页现价已换成实时价，留着会出现「说在下方、其实已跌破」的反话，
 *  所以列表页剥掉第一段（触发价另有 sellTriggerText 现算）。诊断详情页口径自洽，传 withLine=true 原样给。 */
function legacyConviction(r, withLine) {
  const bits = String(r?.suggestion || "").split("｜").map((s) => s.trim()).filter(Boolean);
  if (!bits.length) return "";
  if (!withLine) bits.shift();
  return bits.length ? `可信度 · ${bits.join("｜")}` : "";
}

/** 可信度主行：接口给了结构化百分比就是「可信度 62%」，否则退回老的 suggestion 文案（不回归）。
 *  详情页需要卖点线段时传 withLine=true。 */
export function convictionHead(r, withLine = false) {
  const pct = convictionPct(r);
  return pct === null ? legacyConviction(r, withLine) : `可信度 ${Math.round(pct)}%`;
}

/** 等级徽章文案（高/中/低）；给了不认识的等级就不出徽章。 */
export function convictionLevelText(r) {
  const lv = String(r?.convictionLevel || "").trim();
  return CONVICTION_LEVELS[lv] ? lv : "";
}

/** 等级徽章的样式类（high/mid/low），与 convictionLevelText 成对使用。 */
export function convictionLevelClass(r) {
  const lv = convictionLevelText(r);
  return lv ? CONVICTION_LEVELS[lv] : "";
}

/** 短说明：结构化模式下是接口给的一句话；退回老文案时它已并入主行，这里返回空以免重复。 */
export function convictionNote(r) {
  return convictionPct(r) === null ? "" : String(r?.convictionText || "").trim();
}

/** 可信度整行（主行 + 说明）：详情页 withLine=true 时保留卖点线段。 */
export function convictionText(r, withLine = false) {
  return [convictionHead(r, withLine), convictionNote(r)].filter(Boolean).join(" · ");
}

/** 可信度块是否有内容：任一子行有东西才渲染整块，全空时不出，免得留一段空边距。
 *  筹码价位算不出时插件只给一句 level_note（「近 60 日历史不足」），那句话也得让块渲染出来，
 *  否则使用者读到的是"这一栏没有"，而实际是"这一栏没算成"。 */
export function hasConviction(r, withLine = false) {
  return Boolean(convictionText(r, withLine) || convictionLevelText(r)
    || sellTriggerText(r) || takeProfitText(r) || gameText(r)
    || buyLevelText(r) || levelNoteText(r));
}

/** 止盈价格线：跟 sellTriggerText 一个口径——目标或现价缺一个就整行不显示，有数时按目标在
 *  现价上方/下方选词。页面再冠上「止盈 · 」。 */
export function takeProfitText(r) {
  const tp = num(r?.takeProfit);
  const latest = num(r?.latestPrice);
  if (tp === null || latest === null || latest <= 0) return "";
  const p = tp.toFixed(2);
  if (Math.abs(tp - latest) < 0.005) return `${p}（价格已到位）`;
  return `${tp > latest ? "涨到" : "回落到"} ${p}`;
}

/** 博弈参考行：V/S 是接口给的参考数值、game_text 是定性描述，整体定位比可信度次一级。
 *  全缺就返回空串（整行不显示）。 */
export function gameText(r) {
  const v = num(r?.gameV), s = num(r?.gameS);
  const vs = [];
  if (v !== null) vs.push(`V ${v.toFixed(2)}`);
  if (s !== null) vs.push(`S ${s.toFixed(2)}`);
  const parts = [];
  if (vs.length) parts.push(`${vs.join(" · ")}（参考值）`);
  const txt = String(r?.gameText || "").trim();
  if (txt) parts.push(`定性：${txt}`);
  return parts.length ? `博弈参考 · ${parts.join(" · ")}` : "";
}

/** 持仓总览页的「当日榜内名次」一行话。
 *
 *  名次来自选股引擎对**当天整个票池**的打分，和选股名单里的 rank_no 不是一个口径：
 *  后者只在那一批返回的 topk 行里唯一（默认 30 行），前者是 300 只池子里的位次。
 *  诊断明细页不接这一行：那是历史某一次的诊断，和"当日"名次放一起会把两个日期混成一句。
 *
 *  页头另有一句口径说明，负责两种整批情况：引擎不提供名次（自研内核没有全池榜）、查询失败。
 *  这里只管一只票三种情况，而且**都不补猜测名次**（写不出「99+」「约 301 名」这种东西）：
 *  1. 不在候选池（基金、ST、上市太新、成交额太薄）：模型从没给这只打过分，不是它排在池子后面；
 *  2. 在池里但当日没有可打分的行情（停牌/日线断档）：**最容易被读成「跌出榜外该卖了」**，
 *     所以要把"没行情"说明白；
 *  3. entry 还没到（名次是列表之后异步补的）：返回空串，这一行先不显示。
 */
export function dayRankText(entry, meta) {
  if (!entry || !meta) return "";
  const pool = num(meta.pool_size);
  if (entry.in_pool === false) {
    return `榜内名次 · 不在当前${pool ? ` ${pool} 只` : ""}候选池里，模型没给这只打过分`;
  }
  const rank = num(entry.rank);
  if (rank === null) {
    return "榜内名次 · 在候选池里，但当日没有可打分的行情（停牌或日线断档）";
  }
  const total = num(meta.universe);
  const parts = [`第 ${rank} 名${total ? ` / 共 ${total} 只` : ""}`];
  const exit = num(meta.exit_rank);
  // 卖出线来自回测的 band 纪律（同一个缺省值），所以页面能说"要不要按这条线卖"，
  // 但这是参照不是结论：线本身还没经过样本外确认
  if (exit !== null) parts.push(rank <= exit ? `还在卖出线（前 ${exit} 名）内` : `已跌出卖出线（前 ${exit} 名）`);
  return `榜内名次 · ${parts.join(" · ")}`;
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
    // 第一止盈价 + 价位口径句子（qlib 榜按近 60 日筹码主峰给：下沿买入、上沿止盈）
    sellTarget: num(s.sell_target_price), levelNote: s.level_note || "",
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
    // 引擎显式声明「这一项我不算」的字段名清单（qlib 插件给，自研内核给空）。
    // 空值本身分不出「不算」和「算了但判不出」，而这两件事在页面上必须分开说。
    naFields: String(s.na_fields || "").split(",").map((x) => x.trim()).filter(Boolean),
  };
}

/** 这一行的这个指标是不是「引擎压根不算」。 */
export function isNa(r, field) {
  return Array.isArray(r?.naFields) && r.naFields.includes(field);
}

/** 依据一行话（与诊断页 `可信度 · …` 同一措辞风格）。缺 verdict 时不能整行消失——
 *  用户看到"没有这一行"会理解成"没问题"，而实际是"还没测"。
 *  「还没测」又分两种：判据后验表没建（自研内核），和这个引擎根本没有判据（qlib 只出排序分），
 *  后者若沿用前者的措辞等于给了一句话不成立的解释。 */
export function posteriorText(r) {
  const verdict = (r?.posteriorVerdict || "").trim();
  if (!verdict) {
    return isNa(r, "posterior_verdict")
      ? "依据 · 不适用（该引擎只出排序分，没有判据后验）"
      : "依据 · 未标定（判据后验表还没建）";
  }
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

/** 买点文案：两个位都来自策略自己的判据，缺哪个就不说哪个；都没有就整行不显示。
 *  例外是引擎显式声明不算买点（qlib 没有均线/箱沿这类判据）——那时必须显示一句不适用，
 *  因为「没有失效位」看起来像"这笔买入不会错"，而实际是"没人给你算过"。
 *  措辞别说"该引擎不出价"：同一张卡片上方就显示着基准价（信号日收盘），
 *  那句话当场自相矛盾；这里缺的是**判据价位**，不是价格。 */
export function buyLevelText(r) {
  const ref = num(r?.buyRef);
  const target = num(r?.sellTarget);
  const stop = num(r?.buyInvalid);
  const parts = [];
  if (ref !== null) parts.push(`买入参考 ${ref.toFixed(2)}`);
  if (target !== null) parts.push(`第一止盈 ${target.toFixed(2)}`);
  if (stop !== null) parts.push(`跌破 ${stop.toFixed(2)} 失效`);
  if (!parts.length && isNa(r, "buy_ref_price")) {
    return "买点参考位/失效位 · 不适用（该引擎只出排序分，不给价位；能对照的只有上方基准价）";
  }
  return parts.join(" · ");
}

/** 三档价位的口径：插件给的这句话就是「这些数是怎么来的」，必须原样显示。
 *  光给三个数字，使用者会把它读成模型预测的价格——而那三条其实是一条固定规则。 */
export function levelNoteText(r) {
  return r?.levelNote || "";
}

/** 买入风险分的展示文本（百分点，带符号）。没值给占位符——0 是"预测跑平当日全市场"，
 *  和"闸门没判出来"是两回事，不能拿 0 当缺省。
 *  na=true 是引擎声明「这个数我不算」，得写成「不适用」而不是「—」：
 *  前者说"这里没有预测"，后者看着像"预测约等于零"。 */
export function riskText(v, na = false) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return na ? "不适用" : "—";
  const n = Number(v);
  return `${n > 0 ? "+" : ""}${n.toFixed(2)}pp`;
}

/** 红涨绿跌（A 股习惯）：预测超额为正给 .up，为负给 .down，缺值/不适用不给颜色。 */
export function riskClass(v, na = false) {
  if (na || v === null || v === undefined || Number.isNaN(Number(v))) return "";
  return Number(v) >= 0 ? "up" : "down";
}

/**
 * 排序分：引擎落库的是「这只票在当日票池里的位置」（0~1，第一名正好 1.000），
 * 使用者要看的是满分 100 的刻度，所以乘法只在展示层做一次，落库口径不动（两条榜同一个口径）。
 * 刻度换了，含义没换：它只决定谁排在谁前面，不是把握的大小，所以越界的脏数据夹在 0~100，
 * 而不是原样印出一个 120 分。
 */
export function scoreText(r) {
  const v = num(r && r.score);
  if (v === null || Number.isNaN(v)) return "—";
  const pct = Math.max(0, Math.min(100, Math.round(v * 1000) / 10));
  return pct % 1 === 0 ? String(pct) : pct.toFixed(1);
}

/** 结果页展示顺序：qlib 模型榜先按护栏后名次升序，其他榜再按「预期 10 日超额」降序。
 *
 *  qlib 的 risk 全是 null，不能让它跟着其他榜的风险分混排；模型榜的 #1 必须是页面第一行，
 *  否则同一个任务里旧 hot_pref 的 #1 会排在模型 #1 前面。自研策略仍按风险分排，
 *  因为 box_oscillate 的强度分饱和到 1.000，rank 头部不携带信息（2026-10-11 实测榜首是货币 ETF）。
 *  缺风险分排最后而不是排 0，因为"闸门没判出来"和"预测跑平大盘"是两回事。
 */
export function sortByRiskDesc(rows) {
  const isModel = (r) => r?.strategy === "qlib_alpha158";
  const value = (r) => {
    const v = r?.risk;
    if (v === null || v === undefined || Number.isNaN(Number(v))) return -Infinity;
    return Number(v);
  };
  const rank = (r) => {
    const v = r?.rank;
    return v === null || v === undefined || Number.isNaN(Number(v)) ? Infinity : Number(v);
  };
  return [...rows].sort((a, b) => {
    if (isModel(a) !== isModel(b)) return isModel(a) ? -1 : 1;
    if (isModel(a)) {
      const ar = rank(a), br = rank(b);
      if (ar !== br) return ar - br;
      return String(a?.code || "").localeCompare(String(b?.code || ""));
    }
    // 两个都没判分时 y-x 会是 NaN（规范按 0 处理，但不依赖实现），显式返回 0 让它们保序
    return value(a) === value(b) ? 0 : value(b) - value(a);
  });
}

/** 插件 /strategy-detail 的一行 → 前端选项。中文标签只来自 description，不硬编码策略名。 */
export function toStrategyOption(d) {
  return {
    // 插件显式给 label 就用它（两条榜并存时，芯片上的字要说得清是哪条榜）；
    // 没给才退回从 description 抠「：」前缀的老办法。
    name: d.name || "",
    label: d.label || (d.description || "").split("：")[0] || d.name || "",
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

/** 消息面四类的展示口径：标签和颜色在前端固定，方向/强度/理由全部来自服务端。 */
export const NEWS_DIRS = [
  { key: "bearish", label: "利空", text: "利空", cls: "neg" },
  { key: "risk", label: "风险", text: "风险", cls: "warn" },
  { key: "bullish", label: "利好", text: "利好", cls: "pos" },
  { key: "watch", label: "关注", text: "关注", cls: "neu" },
];

const NEWS_DIR = Object.fromEntries(NEWS_DIRS.map((d) => [d.key, d]));

/** code 只有六位数字才是个股；BK 开头的是板块码，不能拿去开个股 K 线。 */
export function toNewsItem(r) {
  const d = NEWS_DIR[r.direction] || { text: r.direction || "", cls: "neu" };
  const isStock = /^\d{6}$/.test(String(r.code || ""));
  const score = num(r.score) ?? 0;
  return {
    code: r.code || "",
    name: r.name || r.code || "—",
    category: r.category || "",
    direction: r.direction || "",
    dirText: d.text, dirCls: d.cls,
    score,
    scoreText: r.direction === "watch" ? (r.priority || "中") : `${score > 0 ? "+" : ""}${score}`,
    reason: r.reason || "",
    newsCount: num(r.news_count) ?? 0,
    holding: r.holding === true,
    isStock,
  };
}

/**
 * 顶部健康条：批次缺失、批次过期、LLM 分析失败率高都要出声。
 * 这条是「静默空转」的最后防线——没内容可看时，至少要让人知道为什么。
 */
export function newsBanner(data) {
  if (!data || !data.day) {
    return { text: "近 5 天没有成功的消息面批次：LLM 分析可能全失败或调度没跑", cls: "bad" };
  }
  const h = data.health || {};
  const rate = num(h.fail_rate);
  if (rate !== null && rate > 0.2) {
    return { text: `${data.day} 批次：分析失败 ${h.failed ?? 0}/${h.analyzed ?? 0} 条，清单可能不全`, cls: "bad" };
  }
  const stale = num(data.stale_days);
  if (stale !== null && stale > 0) {
    return { text: `这是 ${data.day} 的批次（${stale} 天前）`, cls: "muted" };
  }
  return { text: "", cls: "" };
}
