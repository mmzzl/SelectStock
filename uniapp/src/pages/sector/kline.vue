<!-- uniapp/src/pages/sector/kline.vue —— 个股日 K：手写 canvas 蜡烛图（项目零图表库），几何算在 logic/candles.js -->
<script setup>
import { computed, getCurrentInstance, nextTick, ref } from "vue";
import { onLoad } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";
import { buildCandles, klineSummary, CANDLE_COLORS } from "../../logic/candles.js";
import { signedPct } from "../../logic/view.js";

const { sector } = useClient();
const { proxy } = getCurrentInstance();

const DAY_OPTIONS = [60, 120, 250];
const code = ref("");
const name = ref("");
const days = ref(120);
const bars = ref([]);
const err = ref("");
const canvasW = ref(320);

const CANVAS_H = 260;
const sysW = (() => {
  try { return uni.getSystemInfoSync().windowWidth; } catch { return 375; }
})();
canvasW.value = Math.round(sysW - (sysW * 48) / 750);   // 页面左右各 24rpx 内边距

async function load() {
  err.value = "";
  try {
    const d = await sector.kline(code.value, days.value);
    name.value = d.name || name.value;
    bars.value = d.bars || [];
    if (name.value) uni.setNavigationBarTitle({ title: `${name.value} ${code.value}` });
    await nextTick();
    draw();
  } catch (e) {
    err.value = e.message || "K线加载失败";
    bars.value = [];
  }
}

onLoad((q) => {
  code.value = q.code || "";
  name.value = decodeURIComponent(q.name || "");
  if (q.name) uni.setNavigationBarTitle({ title: `${name.value} ${code.value}` });
  load();
});

function pickDays(d) {
  if (d === days.value) return;
  days.value = d;
  load();
}

function draw() {
  const geo = buildCandles(bars.value, { width: canvasW.value, height: CANVAS_H });
  const ctx = uni.createCanvasContext("klines", proxy);
  ctx.clearRect(0, 0, canvasW.value, CANVAS_H);
  if (!geo.n) { ctx.draw(); return; }

  ctx.setLineWidth(1);
  ctx.setFontSize(10);
  for (const g of geo.grid) {
    ctx.setStrokeStyle("#eef0f5");
    ctx.beginPath();
    ctx.moveTo(0, g.y);
    ctx.lineTo(canvasW.value - 46, g.y);
    ctx.stroke();
    ctx.setFillStyle(CANDLE_COLORS.text);
    ctx.fillText(g.label, canvasW.value - 42, g.y + 4);
  }

  for (const c of geo.candles) {
    ctx.setStrokeStyle(c.color);
    ctx.setLineWidth(1);
    ctx.beginPath();
    ctx.moveTo(c.x, c.wickTop);
    ctx.lineTo(c.x, c.wickBottom);
    ctx.stroke();
    ctx.setFillStyle(c.color);
    ctx.fillRect(c.x - geo.bodyW / 2, c.bodyY, geo.bodyW, c.bodyH);
  }

  for (const s of geo.ma) {
    ctx.setStrokeStyle(s.color);
    ctx.setLineWidth(1);
    ctx.beginPath();
    s.points.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
    ctx.stroke();
  }

  ctx.setStrokeStyle("#dfe3ea");
  ctx.beginPath();
  ctx.moveTo(0, geo.volTop + geo.volH);
  ctx.lineTo(canvasW.value - 46, geo.volTop + geo.volH);
  ctx.stroke();
  for (const v of geo.vol) {
    ctx.setFillStyle(v.color);
    ctx.fillRect(v.x - geo.bodyW / 2, v.y, geo.bodyW, Math.max(v.h, 1));
  }

  ctx.setFillStyle(CANDLE_COLORS.text);
  ctx.setFontSize(10);
  const first = geo.candles[0], last = geo.candles[geo.candles.length - 1];
  if (first) ctx.fillText(String(first.date || "").slice(5), 2, CANVAS_H - 4);
  if (last) ctx.fillText(String(last.date || "").slice(5), canvasW.value - 60, CANVAS_H - 4);
  ctx.draw();
}

const last = computed(() => bars.value[bars.value.length - 1] || null);
const span = computed(() => klineSummary(bars.value));
const cls = (v) => (v === null || v === undefined || Number(v) === 0 ? "" : Number(v) > 0 ? "up" : "down");
</script>
<template>
  <view class="p">
    <view class="segs">
      <text v-for="d in DAY_OPTIONS" :key="d" class="seg" :class="{ on: d === days }" @click="pickDays(d)">近{{ d }}日</text>
    </view>

    <text v-if="err" class="err">{{ err }}</text>

    <view v-if="last" class="card">
      <view class="r1">
        <text class="px">{{ last.close }}</text>
        <text class="chg" :class="cls(last.pct)">{{ signedPct(last.pct) }}</text>
        <text class="who">{{ name }} {{ code }}</text>
      </view>
      <view class="ma">
        <text class="m5">MA5 {{ last.ma5 === null ? '—' : Number(last.ma5).toFixed(2) }}</text>
        <text class="m10">MA10 {{ last.ma10 === null ? '—' : Number(last.ma10).toFixed(2) }}</text>
        <text class="m20">MA20 {{ last.ma20 === null ? '—' : Number(last.ma20).toFixed(2) }}</text>
      </view>
      <text class="rng">{{ span.from }} ~ {{ span.to }} · {{ span.count }}根（成交量单位：手）</text>
    </view>

    <view v-if="bars.length" class="wrap">
      <canvas canvas-id="klines" id="klines" class="cv" :style="{ width: canvasW + 'px', height: '260px' }"></canvas>
    </view>
    <view v-else-if="!err" class="empty">{{ '还没有这只票的日线' }}</view>
  </view>
</template>
<style>
.p { min-height: 100vh; background: #f4f6fb; padding: 20rpx 24rpx 60rpx; box-sizing: border-box; }
.segs { display: flex; gap: 16rpx; margin-bottom: 20rpx; }
.seg { flex: 1; text-align: center; font-size: 26rpx; color: #5b6270; background: #fff; border-radius: 14rpx; padding: 14rpx 0; }
.seg.on { color: #fff; background: #2b5cff; font-weight: 600; }
.err { display: block; font-size: 26rpx; color: #e64545; margin-bottom: 16rpx; }
.card { background: #fff; border-radius: 20rpx; padding: 24rpx; margin-bottom: 18rpx; }
.r1 { display: flex; align-items: baseline; gap: 16rpx; }
.r1 .px { font-size: 44rpx; font-weight: 700; color: #1f2329; }
.r1 .chg { font-size: 28rpx; font-weight: 600; }
.r1 .who { flex: 1; text-align: right; font-size: 24rpx; color: #8a919f; }
.ma { display: flex; gap: 24rpx; margin-top: 12rpx; font-size: 22rpx; }
.ma .m5 { color: #f0a020; }
.ma .m10 { color: #2b5cff; }
.ma .m20 { color: #8a5cf6; }
.rng { display: block; margin-top: 10rpx; font-size: 22rpx; color: #a2a9b5; }
.wrap { background: #fff; border-radius: 20rpx; padding: 20rpx; }
.cv { display: block; }
.empty { background: #fff; border-radius: 20rpx; padding: 60rpx 0; text-align: center; color: #a2a9b5; font-size: 26rpx; }
.up { color: #e64545; }
.down { color: #1fa35f; }
</style>
