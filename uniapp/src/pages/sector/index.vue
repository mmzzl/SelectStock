<!-- uniapp/src/pages/sector/index.vue —— 板块热力图：格子颜色只表示区间涨幅，其余指标进详情页 -->
<script setup>
import { computed, ref } from "vue";
import { onShow } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";
import { toBoardTile } from "../../logic/view.js";

const { sector } = useClient();
const WINDOWS = [3, 7, 30];
const KINDS = [{ key: "industry", label: "行业" }, { key: "concept", label: "概念" }];
const PREVIEW = 60;

const win = ref(3);
const kind = ref("industry");
const asof = ref("");
const windows = ref(WINDOWS);
const tiles = ref([]);
const err = ref("");
const loading = ref(false);
const showAll = ref(false);

async function load() {
  loading.value = true;
  err.value = "";
  try {
    const d = await sector.heat({ window: win.value, kind: kind.value });
    asof.value = d.asof || "";
    if (Array.isArray(d.windows) && d.windows.length) windows.value = d.windows;
    tiles.value = (d.boards || []).map(toBoardTile);
  } catch (e) {
    err.value = e.message || "热力图加载失败";
    tiles.value = [];
  } finally {
    loading.value = false;
  }
}

onShow(load);

function pickWindow(w) {
  if (w === win.value) return;
  win.value = w;
  showAll.value = false;
  load();
}
function pickKind(k) {
  if (k === kind.value) return;
  kind.value = k;
  showAll.value = false;
  load();
}

const visible = computed(() => (showAll.value ? tiles.value : tiles.value.slice(0, PREVIEW)));
const hidden = computed(() => tiles.value.length - visible.value.length);

function goBoard(t) {
  uni.navigateTo({
    url: `/pages/sector/board?code=${encodeURIComponent(t.code)}&kind=${t.kind}`
      + `&window=${win.value}&name=${encodeURIComponent(t.name)}`,
  });
}
</script>
<template>
  <view class="p">
    <view class="segs">
      <text v-for="w in windows" :key="w" class="seg" :class="{ on: w === win }" @click="pickWindow(w)">近{{ w }}日</text>
    </view>
    <view class="tabs">
      <text v-for="k in KINDS" :key="k.key" class="tab" :class="{ on: k.key === kind }" @click="pickKind(k.key)">{{ k.label }}</text>
      <text class="date" v-if="asof">数据日 {{ asof }}</text>
    </view>

    <text class="hint">颜色 = 近{{ win }}个交易日等权涨幅（红涨绿跌），点格子看成分股</text>

    <text v-if="err" class="err">{{ err }}</text>
    <view v-else-if="loading && !tiles.length" class="empty">加载中…</view>
    <view v-else-if="!tiles.length" class="empty">还没有热力图数据，等服务端每日 18:30 计算完成</view>

    <view v-else class="grid">
      <view v-for="t in visible" :key="t.kind + t.code" class="tile" :style="t.heat" @click="goBoard(t)">
        <text class="tn">{{ t.name }}</text>
        <text class="tv">{{ t.retText }}</text>
        <text class="tc">{{ t.stockCount }}/{{ t.memberCount }}只</text>
      </view>
    </view>

    <view v-if="hidden > 0" class="more" @click="showAll = true">展开全部 {{ tiles.length }} 个板块（还有 {{ hidden }} 个）</view>
    <view v-else-if="showAll && tiles.length > PREVIEW" class="more" @click="showAll = false">收起</view>
  </view>
</template>
<style>
.p { min-height: 100vh; background: #f4f6fb; padding: 20rpx 24rpx 60rpx; box-sizing: border-box; }
.segs { display: flex; gap: 16rpx; }
.seg {
  flex: 1; text-align: center; font-size: 26rpx; color: #5b6270;
  background: #fff; border-radius: 14rpx; padding: 14rpx 0;
}
.seg.on { color: #fff; background: #2b5cff; font-weight: 600; }
.tabs { display: flex; align-items: center; margin-top: 18rpx; }
.tab { font-size: 28rpx; color: #8a919f; margin-right: 28rpx; padding-bottom: 6rpx; }
.tab.on { color: #1f2329; font-weight: 700; border-bottom: 4rpx solid #2b5cff; }
.tabs .date { flex: 1; text-align: right; font-size: 22rpx; color: #a2a9b5; }
.hint { display: block; font-size: 22rpx; color: #8a919f; margin: 16rpx 0 18rpx; }
.err { display: block; font-size: 26rpx; color: #e64545; padding: 24rpx 0; }
.empty { background: #fff; border-radius: 20rpx; padding: 60rpx 0; text-align: center; color: #a2a9b5; font-size: 26rpx; }
.grid { display: flex; flex-wrap: wrap; }
.tile {
  width: 31.6%; margin: 0 2.6% 18rpx 0; border-radius: 14rpx;
  padding: 16rpx 12rpx; box-sizing: border-box; min-height: 132rpx;
  display: flex; flex-direction: column; justify-content: space-between;
}
.tile:nth-child(3n) { margin-right: 0; }
.tile .tn { font-size: 24rpx; line-height: 1.25; opacity: .95; }
.tile .tv { font-size: 30rpx; font-weight: 700; margin-top: 6rpx; }
.tile .tc { font-size: 20rpx; opacity: .8; }
.more {
  background: #fff; border-radius: 16rpx; padding: 24rpx 0; margin-top: 8rpx;
  text-align: center; color: #2b5cff; font-size: 26rpx;
}
</style>
