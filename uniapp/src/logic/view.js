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
  return {
    code: s.code, name: s.name || s.code, signal: s.signal || "", reason: s.reason || "",
    // 选股插件 P1 富化字段；后端列名 rank_no（rank 是 SQL 保留字），这里统一回 rank
    strategy: s.strategy || s.signal || "", asof: s.asof || "",
    price: num(s.price), score: num(s.score), rank: num(s.rank ?? s.rank_no),
  };
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