<!-- uniapp/src/pages/news/feed.vue —— 实时消息：东财 10 秒线的原始流，只做展示，不判方向 -->
<script setup>
import { ref } from "vue";
import { onPullDownRefresh, onReachBottom, onShow } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";
import { toNewsFeedRow } from "../../logic/view.js";

const { news } = useClient();

const PAGE = 30;
const rows = ref([]);
const cursor = ref(null);
const err = ref("");
const loading = ref(false);
const exhausted = ref(false);

async function fetchPage(reset) {
  if (loading.value) return;
  loading.value = true;
  err.value = "";
  try {
    const d = await news.feed({ limit: PAGE, before: reset ? "" : (cursor.value || "") });
    const page = (d.rows || []).map(toNewsFeedRow);
    rows.value = reset ? page : [...rows.value, ...page];
    cursor.value = d.next_cursor || null;
    exhausted.value = !cursor.value;
  } catch (e) {
    err.value = e.message || "实时消息加载失败";
    if (reset) rows.value = [];
  } finally {
    loading.value = false;
  }
}

onShow(() => {
  if (!rows.value.length) fetchPage(true);
});

onPullDownRefresh(async () => {
  await fetchPage(true);
  uni.stopPullDownRefresh();
});

onReachBottom(() => {
  if (!exhausted.value) fetchPage(false);
});

function goDetail(r) {
  uni.navigateTo({ url: `/pages/news/detail?id=${r.id}` });
}
</script>
<template>
  <view class="p">
    <view class="hd">
      <text class="dt">东财实时消息 · 已载入 {{ rows.length }} 条</text>
      <text class="rf" @click="fetchPage(true)">{{ loading ? "加载中…" : "刷新" }}</text>
    </view>
    <text class="tip">原文来自东方财富 10 秒线，每 30 秒采一批。方向与影响判断请看「消息面」，这里不判。</text>
    <text v-if="err" class="err">{{ err }}</text>

    <view v-if="loading && !rows.length" class="empty">加载中…</view>
    <view v-else-if="!rows.length && !err" class="empty">还没有采到消息</view>
    <view v-else class="sec">
      <view v-for="r in rows" :key="r.id" class="row" @click="goDetail(r)">
        <text class="tt">{{ r.title }}</text>
        <text class="sm">{{ r.summary }}</text>
        <view class="ft">
          <text class="tm">{{ r.time }}</text>
          <text v-for="s in r.stocks" :key="s" class="stk">{{ s }}</text>
          <text v-for="b in r.boards" :key="b" class="brd">{{ b }}</text>
        </view>
      </view>
    </view>
    <view v-if="rows.length && exhausted" class="foot">已到最早一条</view>
  </view>
</template>
<style>
.p { min-height: 100vh; background: #f4f6fb; padding: 20rpx 24rpx 60rpx; box-sizing: border-box; }
.hd { display: flex; align-items: center; }
.hd .dt { flex: 1; font-size: 28rpx; font-weight: 700; color: #1f2329; }
.hd .rf { font-size: 26rpx; color: #2b5cff; padding: 6rpx 12rpx; }
.tip { display: block; font-size: 22rpx; color: #8a919f; margin-top: 8rpx; line-height: 1.5; }
.err { display: block; font-size: 26rpx; color: #e64545; padding: 20rpx 0; }
.empty { background: #fff; border-radius: 20rpx; padding: 48rpx 0; text-align: center; color: #a2a9b5; font-size: 26rpx; margin-top: 20rpx; }
.sec { background: #fff; border-radius: 20rpx; overflow: hidden; margin-top: 20rpx; }
.row { padding: 22rpx 24rpx; border-bottom: 2rpx solid #f0f2f7; }
.row:last-child { border-bottom: none; }
.row:active { background: #f7f9ff; }
.tt { display: block; font-size: 28rpx; color: #1f2329; font-weight: 600; line-height: 1.4; }
.sm { display: block; font-size: 24rpx; color: #5b6270; line-height: 1.5; margin-top: 8rpx; }
.ft { display: flex; align-items: center; margin-top: 10rpx; }
.ft .tm { font-size: 20rpx; color: #a2a9b5; }
.ft .stk { font-size: 20rpx; color: #2b5cff; background: #eaf0ff; border-radius: 8rpx; padding: 2rpx 12rpx; margin-left: 12rpx; }
.ft .brd { font-size: 20rpx; color: #8a6d1f; background: #fdf6e3; border-radius: 8rpx; padding: 2rpx 12rpx; margin-left: 12rpx; }
.foot { text-align: center; color: #a2a9b5; font-size: 22rpx; margin-top: 24rpx; }
</style>
