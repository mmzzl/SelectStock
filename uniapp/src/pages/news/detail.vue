<!-- uniapp/src/pages/news/detail.vue —— 一条实时消息的原文：标题 + 摘要 + 关联标的 -->
<script setup>
import { ref } from "vue";
import { onLoad } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";
import { toNewsFeedRow } from "../../logic/view.js";

const { news } = useClient();
const id = ref("");
const row = ref(null);
const counts = ref({ pinglun: 0, share: 0 });
const err = ref("");
const loading = ref(false);

async function load() {
  loading.value = true;
  err.value = "";
  try {
    const d = await news.item(id.value);
    row.value = toNewsFeedRow(d);
    counts.value = { pinglun: d.pinglun_Num || 0, share: d.share || 0 };
  } catch (e) {
    err.value = e.message || "消息加载失败";
    row.value = null;
  } finally {
    loading.value = false;
  }
}

onLoad((q) => {
  id.value = (q && q.id) || "";
  if (!id.value) {
    err.value = "缺少消息 id";
    return;
  }
  load();
});

function goStock(code) {
  uni.navigateTo({ url: `/pages/sector/kline?code=${code}` });
}
</script>
<template>
  <view class="p">
    <text v-if="err" class="err">{{ err }}</text>
    <view v-else-if="loading && !row" class="empty">加载中…</view>
    <view v-else-if="row" class="card">
      <text class="tt">{{ row.title }}</text>
      <text class="mt">{{ row.time }} · 评论 {{ counts.pinglun }} · 分享 {{ counts.share }}</text>
      <text class="sm">{{ row.summary }}</text>
      <view v-if="row.stocks.length || row.boards.length" class="tags">
        <text class="lb">关联</text>
        <text v-for="s in row.stocks" :key="s" class="stk" @click="goStock(s)">{{ s }}</text>
        <text v-for="b in row.boards" :key="b" class="brd">{{ b }}</text>
      </view>
      <view class="foot">原文摘自东方财富实时消息，未经改写；判断与持仓相关性请看「消息面」。</view>
    </view>
  </view>
</template>
<style>
.p { min-height: 100vh; background: #f4f6fb; padding: 20rpx 24rpx 60rpx; box-sizing: border-box; }
.err { display: block; font-size: 26rpx; color: #e64545; padding: 20rpx 0; }
.empty { background: #fff; border-radius: 20rpx; padding: 48rpx 0; text-align: center; color: #a2a9b5; font-size: 26rpx; }
.card { background: #fff; border-radius: 20rpx; padding: 28rpx 24rpx; }
.tt { display: block; font-size: 34rpx; font-weight: 700; color: #1f2329; line-height: 1.4; }
.mt { display: block; font-size: 22rpx; color: #a2a9b5; margin-top: 12rpx; }
.sm { display: block; font-size: 28rpx; color: #333a45; line-height: 1.7; margin-top: 20rpx; }
.tags { display: flex; flex-wrap: wrap; align-items: center; margin-top: 24rpx; }
.tags .lb { font-size: 22rpx; color: #8a919f; margin-right: 12rpx; }
.tags .stk { font-size: 22rpx; color: #2b5cff; background: #eaf0ff; border-radius: 8rpx; padding: 4rpx 14rpx; margin: 0 12rpx 8rpx 0; }
.tags .brd { font-size: 22rpx; color: #8a6d1f; background: #fdf6e3; border-radius: 8rpx; padding: 4rpx 14rpx; margin: 0 12rpx 8rpx 0; }
.foot { margin-top: 28rpx; font-size: 22rpx; color: #a2a9b5; line-height: 1.6; }
</style>
