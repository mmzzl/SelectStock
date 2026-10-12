// uniapp/src/logic/candles.js —— 日 K 蜡烛图几何：纯函数、可单测，页面里的 canvas 只负责描图。
// 项目零图表库（不引 echarts/uni-chart），这里把坐标算好，draw 只做 fillRect/moveTo/lineTo。

const num = (v) => (v === undefined || v === null || Number.isNaN(Number(v)) ? null : Number(v));

export const CANDLE_COLORS = {
  up: "#e64545",        // A 股习惯：红涨
  down: "#1fa35f",      // 绿跌
  axis: "#c2c8d2",
  text: "#8a919f",
  ma: { ma5: "#f0a020", ma10: "#2b5cff", ma20: "#8a5cf6" },
};

/**
 * @param {Array} bars  后端 /sector/kline 的 bars（date/open/high/low/close/volume/ma*）
 * @param {Object} box  { width, height, priceRatio } 画布尺寸（px）
 * @return 画布坐标：candles/vol/ma/grid，全部按传入尺寸算好
 */
export function buildCandles(bars, { width, height, priceRatio = 0.74 } = {}) {
  const list = (bars || []).filter((b) => num(b?.close) !== null);
  const w = num(width) || 0;
  const h = num(height) || 0;
  const base = { n: list.length, width: w, height: h, step: 0, bodyW: 0,
                 priceTop: 0, priceH: 0, volTop: 0, volH: 0,
                 min: null, max: null, maxVolume: 0,
                 candles: [], vol: [], ma: [], grid: [], axis: CANDLE_COLORS.axis, text: CANDLE_COLORS.text };
  if (!list.length || w <= 0 || h <= 0) return base;

  const priceH = Math.max(h * priceRatio - 18, 40);
  const priceTop = 10;
  const volH = Math.max(h - priceH - priceTop - 26, 20);
  const volTop = priceTop + priceH + 16;
  const step = w / list.length;
  const bodyW = Math.max(Math.min(step * 0.62, 14), 1.5);

  let min = Infinity, max = -Infinity, maxVolume = 0;
  for (const b of list) {
    const hi = num(b.high) ?? num(b.close), lo = num(b.low) ?? num(b.close);
    if (hi > max) max = hi;
    if (lo < min) min = lo;
    const v = num(b.volume);
    if (v !== null && v > maxVolume) maxVolume = v;
  }
  if (!Number.isFinite(min) || !Number.isFinite(max)) return { ...base, n: list.length };
  const pad = (max - min) * 0.04 || Math.max(max * 0.01, 0.01);
  min -= pad; max += pad;
  const span = max - min || 1;
  const yOf = (p) => priceTop + ((max - p) / span) * priceH;

  const candles = list.map((b, i) => {
    const close = num(b.close);
    const open = num(b.open) ?? close;
    const high = num(b.high) ?? Math.max(open, close);
    const low = num(b.low) ?? Math.min(open, close);
    const up = close >= open;
    const x = i * step + step / 2;
    const yClose = yOf(close), yOpen = yOf(open);
    return {
      x, up, color: up ? CANDLE_COLORS.up : CANDLE_COLORS.down,
      wickTop: yOf(high), wickBottom: yOf(low),
      bodyY: Math.min(yOpen, yClose), bodyH: Math.max(Math.abs(yOpen - yClose), 1),
      close, date: b.date,
    };
  });

  const vol = list.map((b, i) => {
    const v = num(b.volume) || 0;
    const bh = (v / (maxVolume || 1)) * volH;
    const up = (num(b.close) ?? 0) >= (num(b.open) ?? num(b.close) ?? 0);
    return { x: i * step + step / 2, y: volTop + volH - bh, h: bh,
             color: up ? CANDLE_COLORS.up : CANDLE_COLORS.down };
  });

  const ma = Object.keys(CANDLE_COLORS.ma).map((key) => ({
    key, color: CANDLE_COLORS.ma[key],
    points: list.reduce((acc, b, i) => {
      const p = num(b[key]);
      if (p !== null) acc.push({ x: i * step + step / 2, y: yOf(p) });
      return acc;
    }, []),
  })).filter((s) => s.points.length > 1);

  const grid = [0, 1 / 3, 2 / 3, 1].map((f) => {
    const price = max - span * f;
    return { y: priceTop + priceH * f, label: price.toFixed(2) };
  });

  return { ...base, step, bodyW, priceTop, priceH, volTop, volH,
           min, max, maxVolume, candles, vol, ma, grid,
           lastClose: candles[candles.length - 1]?.close ?? null,
           lastY: yOf(list[list.length - 1].close) };
}

/** 首/末交易日与根数，页面顶部一行说明用（不在这里做任何加减乘除）。 */
export function klineSummary(bars) {
  const list = (bars || []).filter((b) => b?.date);
  if (!list.length) return { count: 0, from: "", to: "" };
  return { count: list.length, from: list[0].date, to: list[list.length - 1].date };
}
