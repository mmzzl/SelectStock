import test from "node:test";
import assert from "node:assert/strict";
import { buildCandles, klineSummary, CANDLE_COLORS } from "../src/logic/candles.js";

const bars = (closes, opts = {}) => closes.map((c, i) => ({
  date: `2026-10-${String(i + 1).padStart(2, "0")}`,
  open: opts.opens ? opts.opens[i] : c,
  high: opts.high ?? c + 0.5,
  low: opts.low ?? c - 0.5,
  close: c,
  volume: opts.volumes ? opts.volumes[i] : 100,
  ma5: opts.ma5 ? opts.ma5[i] : null,
  ma10: null, ma20: null,
}));

test("蜡烛几何：按交易日内等分，阳线/阴线取红涨绿跌", () => {
  const geo = buildCandles(bars([10, 11]), { width: 200, height: 200 });
  assert.equal(geo.n, 2);
  assert.equal(geo.step, 100);
  assert.equal(geo.candles[0].x, 50);
  assert.equal(geo.candles[1].x, 150);
  assert.equal(geo.candles[0].color, CANDLE_COLORS.up);      // open==close 记作不跌 → 红
  assert.ok(geo.candles[1].wickTop < geo.candles[1].bodyY);   // 上影线在实体之上（canvas y 向下）
  assert.ok(geo.candles[1].wickBottom > geo.candles[1].bodyY + geo.candles[1].bodyH);
});

test("价格轴：最高/最低定域，收盘跌到最低点时 y 落在底部", () => {
  const geo = buildCandles(bars([10, 20]), { width: 100, height: 100 });
  assert.ok(geo.min < 9.5 && geo.max > 20.5);               // 4% 余量
  assert.ok(geo.candles[0].bodyY > geo.candles[1].bodyY);   // 便宜的价格画在下面
});

test("平盘（最高=最低）不能把价格轴炸成 NaN", () => {
  const geo = buildCandles(bars([10, 10], { high: 10, low: 10 }), { width: 100, height: 100 });
  for (const c of geo.candles) {
    assert.ok(Number.isFinite(c.bodyY) && Number.isFinite(c.wickTop));
    assert.equal(c.bodyH, 1);                                // 十字线也要看得见
  }
});

test("均线只在有值且多于一个点时成线", () => {
  const geo = buildCandles(bars([10, 11, 12], { ma5: [null, 10.5, 11] }), { width: 90, height: 90 });
  assert.equal(geo.ma.length, 1);
  assert.equal(geo.ma[0].key, "ma5");
  assert.equal(geo.ma[0].points.length, 2);                  // 第一个点缺失被跳过
});

test("成交量柱与最大量成正比，且贴在底部区域", () => {
  const geo = buildCandles(bars([10, 11], { volumes: [50, 100] }), { width: 100, height: 200 });
  const [a, b] = geo.vol;
  assert.equal(geo.maxVolume, 100);
  assert.ok(Math.abs(b.h - geo.volH) < 1e-6);
  assert.ok(Math.abs(a.h - geo.volH / 2) < 1e-6);
  assert.ok(b.y + b.h <= geo.volTop + geo.volH + 1e-6);
});

test("空输入 / 零尺寸返回安全几何，不抛异常", () => {
  assert.equal(buildCandles([], { width: 100, height: 100 }).n, 0);
  assert.equal(buildCandles(null, { width: 100, height: 100 }).candles.length, 0);
  assert.equal(buildCandles(bars([10]), { width: 0, height: 0 }).step, 0);
  assert.equal(buildCandles(bars([10]), {}).width, 0);
});

test("缺 high/low 的行用开收盘兜底，不至于画不出", () => {
  const list = [{ date: "2026-10-01", close: 10 }, { date: "2026-10-02", close: 11, open: 10.5 }];
  const geo = buildCandles(list, { width: 100, height: 100 });
  assert.equal(geo.candles.length, 2);
  assert.ok(Number.isFinite(geo.candles[0].wickTop));
});

test("klineSummary：首末日期与根数", () => {
  assert.deepEqual(klineSummary(bars([10, 11, 12])), { count: 3, from: "2026-10-01", to: "2026-10-03" });
  assert.deepEqual(klineSummary([]), { count: 0, from: "", to: "" });
});
