<!-- uniapp/src/pages/news/index.vue —— 消息面：LLM 每日拆出的利空/风险/利好/关注，持仓相关单独置顶 -->
<script setup>
import { computed, ref } from "vue";
import { onShow } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";
import { NEWS_DIRS, newsBanner, toNewsItem } from "../../logic/view.js";

const { news } = useClient();

const EMPTY = { day: "", stale_days: null, counts: {}, health: {}, items: {}, mine: [], holding_count: 0 };

const data = ref({ ...EMPTY });
const dir = ref("bearish");
const err = ref("");
const loading = ref(false);

async function load() {
  loading.value = true;
  err.value = "";
  try {
    const d = await news.daily();
    const items = {};
    for (const k of NEWS_DIRS) items[k.key] = ((d.items || {})[k.key] || []).map(toNewsItem);
    data.value = {
      ...EMPTY, ...d, items,
      mine: (d.mine || []).map(toNewsItem),
    };
  } catch (e) {
    err.value = e.message || "消息面加载失败";
    data.value = { ...EMPTY };
  } finally {
    loading.value = false;
  }
}

onShow(load);

const banner = computed(() => newsBanner(data.value));
const rows = computed(() => data.value.items[dir.value] || []);
const countOf = (k) => (data.value.counts || {})[k] || 0;

// 板块条目（BK 码）没有可点的个股图，只有六位代码的个股能跳 K 线
function goStock(it) {
  if (!it.isStock) return;
  uni.navigateTo({ url: `/pages/sector/kline?code=${it.code}&name=${encodeURIComponent(it.name)}` });
}

function goFeed() {
  uni.navigateTo({ url: "/pages/news/feed" });
}
</script>
<template>
  <view class="p">
    <view class="hd">
      <text class="dt">{{ data.day ? `${data.day} 批次` : "暂无批次" }}</text>
      <text class="rf" @click="goFeed">实时消息</text>
      <text class="rf" @click="load">{{ loading ? "刷新中…" : "刷新" }}</text>
    </view>

    <text v-if="banner.text" class="banner" :class="banner.cls">{{ banner.text }}</text>
    <text v-if="err" class="err">{{ err }}</text>
    <view v-else-if="loading && !data.day" class="empty">加载中…</view>

    <view v-if="data.mine.length" class="sec">
      <view class="st">与我的 {{ data.holding_count }} 只持仓有关</view>
      <view v-for="(it, i) in data.mine" :key="'m' + i" class="row" @click="goStock(it)">
        <view class="l1">
          <text class="tag" :class="it.dirCls">{{ it.dirText }}</text>
          <text class="nm">{{ it.name }}</text>
          <text class="cd" v-if="it.code">{{ it.code }}</text>
          <text class="sc" :class="it.dirCls">{{ it.scoreText }}</text>
        </view>
        <text class="rs">{{ it.reason }}</text>
        <text class="ft" v-if="it.newsCount">{{ it.newsCount }} 条新闻</text>
      </view>
    </view>
    <view v-else-if="data.holding_count > 0 && data.day && !err" class="mine-none">
      当前 {{ data.holding_count }} 只持仓今日没有被拆进消息面（不代表没有风险，只是新闻里没提到）
    </view>

    <view class="tabs">
      <text v-for="k in NEWS_DIRS" :key="k.key" class="tab" :class="{ on: k.key === dir }"
            @click="dir = k.key">{{ k.label }} {{ countOf(k.key) }}</text>
    </view>

    <view v-if="!rows.length && !err" class="empty">这一类今天没有条目</view>
    <view v-else class="sec">
      <view v-for="(it, i) in rows" :key="dir + i" class="row" @click="goStock(it)">
        <view class="l1">
          <text class="nm">{{ it.name }}</text>
          <text class="cd" v-if="it.code">{{ it.code }}</text>
          <text class="cat" v-if="it.category">{{ it.category }}</text>
          <text class="hold" v-if="it.holding">持仓</text>
          <text class="sc" :class="it.dirCls">{{ it.scoreText }}</text>
        </view>
        <text class="rs">{{ it.reason }}</text>
        <text class="ft" v-if="it.newsCount">{{ it.newsCount }} 条新闻</text>
      </view>
    </view>

    <view class="foot">方向、强度与理由由每日新闻分析产出，未经历史校验，只作参考</view>
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
.mine-none { background: #fff; border-radius: 16rpx; padding: 24rpx; margin-top: 20rpx; font-size: 24rpx; color: #8a919f; line-height: 1.5; }
.tabs { display: flex; margin: 24rpx 0 16rpx; }
.tab { flex: 1; text-align: center; font-size: 26rpx; color: #8a919f; padding-bottom: 10rpx; }
.tab.on { color: #1f2329; font-weight: 700; border-bottom: 4rpx solid #2b5cff; }
.sec { background: #fff; border-radius: 20rpx; overflow: hidden; }
.sec .st { font-size: 26rpx; font-weight: 700; color: #1f2329; padding: 24rpx 24rpx 6rpx; }
.row { padding: 20rpx 24rpx; border-bottom: 2rpx solid #f0f2f7; }
.row:last-child { border-bottom: none; }
.row:active { background: #f7f9ff; }
.l1 { display: flex; align-items: center; }
.l1 .nm { font-size: 28rpx; color: #1f2329; font-weight: 600; }
.l1 .cd { font-size: 22rpx; color: #a2a9b5; margin-left: 12rpx; }
.l1 .cat { font-size: 20rpx; color: #8a919f; background: #f0f2f7; border-radius: 8rpx; padding: 2rpx 12rpx; margin-left: 12rpx; }
.l1 .hold { font-size: 20rpx; color: #2b5cff; background: #eaf0ff; border-radius: 8rpx; padding: 2rpx 12rpx; margin-left: 12rpx; }
.l1 .sc { margin-left: auto; font-size: 28rpx; font-weight: 700; }
.tag { font-size: 20rpx; border-radius: 8rpx; padding: 2rpx 12rpx; margin-right: 12rpx; }
.tag.neg, .sc.neg { color: #e64545; }
.tag.warn, .sc.warn { color: #e08a00; }
.tag.pos, .sc.pos { color: #0a9c5b; }
.tag.neu, .sc.neu { color: #5b6270; }
.tag { background: #f4f6fb; }
.rs { display: block; font-size: 24rpx; color: #5b6270; line-height: 1.5; margin-top: 8rpx; }
.ft { display: block; font-size: 20rpx; color: #a2a9b5; margin-top: 8rpx; }
.foot { text-align: center; color: #a2a9b5; font-size: 22rpx; margin-top: 32rpx; line-height: 1.5; }
</style>
