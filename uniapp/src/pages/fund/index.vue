<!-- uniapp/src/pages/fund/index.vue —— 主力资金榜：最近 3/7/30 个交易日的净流入/流出排行 -->
<script setup>
import { computed, ref } from "vue";
import { onShow } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";
import { fundHeadline, toFundRow } from "../../logic/view.js";

const { fund } = useClient();

const WINDOW_OPTIONS = [3, 7, 30];
const EMPTY = { asof: null, window: null, windows: [], side: "in", days: 0,
                count: 0, total: 0, rows: [], notice: null };

const data = ref({ ...EMPTY });
const win = ref(3);
const side = ref("in");
const err = ref("");
const loading = ref(false);

async function load() {
  loading.value = true;
  err.value = "";
  try {
    const d = await fund.rank({ window: win.value, side: side.value, limit: 50 });
    data.value = { ...EMPTY, ...d, rows: (d.rows || []).map(toFundRow) };
  } catch (e) {
    err.value = e.message || "资金榜加载失败";
    data.value = { ...EMPTY };
  } finally {
    loading.value = false;
  }
}

onShow(load);

function pickWindow(w) {
  if (w === win.value) return;
  win.value = w;
  load();
}

function pickSide(s) {
  if (s === side.value) return;
  side.value = s;
  load();
}

const headline = computed(() => fundHeadline(data.value));
// 抬头与提示条不重复念同一句：有问题要出声时，抬头退回中性标题
const titleText = computed(() => (headline.value.cls ? "主力资金榜" : headline.value.text));
const rows = computed(() => data.value.rows);
// 三个档位都留着：某一档因为某天采薄了而没有票时，点进去要看见「为什么没有」，
// 而不是让筹码自己消失（缺数不能显示成「没有资金动向」）。
const windows = WINDOW_OPTIONS;

function goStock(r) {
  if (!r.isStock) return;
  uni.navigateTo({ url: `/pages/sector/kline?code=${r.code}&name=${encodeURIComponent(r.name)}` });
}
</script>
<template>
  <view class="p">
    <view class="hd">
      <text class="dt">{{ titleText }}</text>
      <text class="rf" @click="load">{{ loading ? "刷新中…" : "刷新" }}</text>
    </view>
    <text v-if="headline.cls" class="banner" :class="headline.cls">{{ headline.text }}</text>
    <text v-if="err" class="err">{{ err }}</text>

    <view class="tabs">
      <text v-for="w in windows" :key="'w' + w" class="tab" :class="{ on: w === win }"
            @click="pickWindow(w)">近 {{ w }} 日</text>
    </view>
    <view class="sides">
      <text class="side" :class="{ on: side === 'in' }" @click="pickSide('in')">主力流入</text>
      <text class="side out" :class="{ on: side === 'out' }" @click="pickSide('out')">主力流出</text>
    </view>

    <view v-if="loading && !rows.length" class="empty">加载中…</view>
    <view v-else-if="!rows.length && !err" class="empty">这一档没有票</view>
    <view v-else class="sec">
      <view v-for="(r, i) in rows" :key="r.code" class="row" @click="goStock(r)">
        <text class="no">{{ i + 1 }}</text>
        <view class="mid">
          <view class="l1">
            <text class="nm">{{ r.name }}</text>
            <text class="cd">{{ r.code }}</text>
          </view>
          <text class="l2">成交占比 {{ r.ratioText }} · 收盘 {{ r.close ?? "—" }}</text>
        </view>
        <view class="rt">
          <text class="net" :class="r.isInflow ? 'pos' : 'neg'">{{ r.netText }}</text>
          <text class="ret" :class="r.ret >= 0 ? 'pos' : 'neg'">{{ r.retText }}</text>
        </view>
      </view>
    </view>

    <view class="foot">
      主力净额 = 超大单 + 大单净流入（东财口径，单位已换算成亿）；占比 = 窗口净额 ÷ 同期成交额；
      区间涨幅按日涨跌幅复利累乘。窗口内缺一天（停牌/没采到）的票不进榜。
      实测这一指标对次日没有前向信号（FF-1），只作观察，不构成买卖依据。
    </view>
  </view>
</template>
<style>
.p { min-height: 100vh; background: #f4f6fb; padding: 20rpx 24rpx 60rpx; box-sizing: border-box; }
.hd { display: flex; align-items: center; }
.hd .dt { flex: 1; font-size: 28rpx; font-weight: 700; color: #1f2329; }
.hd .rf { font-size: 26rpx; color: #2b5cff; padding: 6rpx 12rpx; }
.banner { display: block; font-size: 24rpx; border-radius: 14rpx; padding: 16rpx 20rpx; margin-top: 16rpx; }
.banner.bad { color: #b3261e; background: #fdeceb; }
.banner.muted { color: #8a6d1f; background: #fdf6e3; }
.err { display: block; font-size: 26rpx; color: #e64545; padding: 20rpx 0; }
.empty { background: #fff; border-radius: 20rpx; padding: 48rpx 0; text-align: center; color: #a2a9b5; font-size: 26rpx; margin-top: 20rpx; }
.tabs { display: flex; margin: 24rpx 0 12rpx; }
.tab { flex: 1; text-align: center; font-size: 26rpx; color: #8a919f; padding-bottom: 10rpx; }
.tab.on { color: #1f2329; font-weight: 700; border-bottom: 4rpx solid #2b5cff; }
.sides { display: flex; margin-bottom: 16rpx; }
.side { flex: 1; text-align: center; font-size: 26rpx; color: #5b6270; background: #fff; border-radius: 14rpx; padding: 14rpx 0; margin-right: 16rpx; }
.side:last-child { margin-right: 0; }
.side.on { color: #fff; background: #e64545; }
.side.out.on { background: #0a9c5b; }
.sec { background: #fff; border-radius: 20rpx; overflow: hidden; }
.row { display: flex; align-items: center; padding: 20rpx 24rpx; border-bottom: 2rpx solid #f0f2f7; }
.row:last-child { border-bottom: none; }
.row:active { background: #f7f9ff; }
.no { width: 44rpx; font-size: 24rpx; color: #a2a9b5; }
.mid { flex: 1; }
.l1 { display: flex; align-items: center; }
.l1 .nm { font-size: 28rpx; color: #1f2329; font-weight: 600; }
.l1 .cd { font-size: 22rpx; color: #a2a9b5; margin-left: 12rpx; }
.l2 { display: block; font-size: 22rpx; color: #8a919f; margin-top: 6rpx; }
.rt { text-align: right; }
.rt .net { display: block; font-size: 30rpx; font-weight: 700; }
.rt .ret { display: block; font-size: 22rpx; margin-top: 6rpx; }
.pos { color: #e64545; }
.neg { color: #0a9c5b; }
.foot { text-align: center; color: #a2a9b5; font-size: 22rpx; margin-top: 32rpx; line-height: 1.6; }
</style>
