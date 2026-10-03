<!-- uniapp/src/pages/profit/index.vue —— 利润分析：纯透传 /holdings/pnl，按盈亏方向分组（R1-3 不计算） -->
<script setup>
import { ref, computed } from "vue";
import { onShow } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";
import { toPnlView } from "../../logic/view.js";

const v = ref({ rows: [], realized: {}, unrealized: {} });
const day = ref(""); const err = ref(""); const loading = ref(false);
const { holdings } = useClient();

async function load() {
  loading.value = true; err.value = "";
  try {
    v.value = toPnlView(await holdings.pnl({ natural_day: day.value }));
  } catch (e) { err.value = e.message || "插件不可用"; } finally { loading.value = false; }
}
onShow(() => { if (!day.value) day.value = new Date().toISOString().slice(0, 10); load(); });

function onPickDay(e) { day.value = e.detail.value; load(); }

const groups = computed(() => {
  const pos = [], neg = [], flat = [];
  for (const r of v.value.rows) {
    if (r.totalPnl === null) flat.push(r);
    else if (r.totalPnl > 0) pos.push(r);
    else if (r.totalPnl < 0) neg.push(r);
    else flat.push(r);
  }
  // 组内按盈亏绝对值大的排前，只是排序不做算术
  const byAbs = (a, b) => Math.abs(b.totalPnl || 0) - Math.abs(a.totalPnl || 0);
  return [
    { key: "pos", label: "盈利", items: pos.sort(byAbs) },
    { key: "neg", label: "亏损", items: neg.sort(byAbs) },
    { key: "flat", label: "持平/待诊断", items: flat },
  ].filter((g) => g.items.length);
});

const money = (x) => (x === null || x === undefined ? "—" : Number(x).toFixed(2));
const cls = (x) => (x > 0 ? "up" : x < 0 ? "down" : "");
const signed = (x) => (x === null || x === undefined ? "—" : (x > 0 ? "+" : "") + money(x));
</script>
<template>
  <view class="p">
    <view class="bar">
      <picker mode="date" :value="day" @change="onPickDay">
        <text>{{ day || "选择日期" }}</text>
      </picker>
      <text class="reason">{{ loading ? "加载中…" : "口径由持仓诊断插件返回" }}</text>
    </view>
    <view v-if="err" class="err">{{ err }}</view>

    <view class="card hero">
      <view class="kv3">
        <view class="k">
          <text class="n" :class="cls(v.unrealized.totalUnrealized)">{{ signed(v.unrealized.totalUnrealized) }}</text>
          <text class="l">未实现盈亏</text>
        </view>
        <view class="k">
          <text class="n">{{ money(v.unrealized.totalValue) }}</text>
          <text class="l">持仓市值</text>
        </view>
        <view class="k">
          <text class="n" :class="cls(v.realized.totalRealized)">{{ signed(v.realized.totalRealized) }}</text>
          <text class="l">已实现盈亏</text>
        </view>
      </view>
      <view class="foot">
        <text>持仓 {{ v.unrealized.holdingCount ?? "—" }} 只</text>
        <text>交易费用 {{ money(v.realized.totalFees) }}</text>
      </view>
    </view>

    <block v-for="g in groups" :key="g.key">
      <view class="grp">
        <text class="t">{{ g.label }}</text>
        <text class="c">{{ g.items.length }} 只</text>
      </view>
      <view v-for="r in g.items" :key="r.code" class="trow">
        <view class="l">
          <text class="d">仓位 {{ r.position ?? "—" }} · 成本 {{ r.avgCost ?? "—" }} · 现价 {{ r.latestPrice ?? "—" }}</text>
          <text class="n">{{ r.name }}（{{ r.code }}）</text>
          <text class="sub">已实现 {{ signed(r.realizedPnl) }} · 浮动 {{ signed(r.unrealizedPnl) }} · 费用 {{ money(r.fees) }}</text>
        </view>
        <view class="r">
          <text class="amt" :class="cls(r.totalPnl)">{{ signed(r.totalPnl) }}</text>
          <text class="q">合计盈亏</text>
        </view>
      </view>
    </block>

    <view v-if="!groups.length && !loading && !err" class="empty">该日期暂无盈亏数据，可在「诊断」页立即跑一次</view>
  </view>
</template>
<style scoped>
.hero { padding: 10rpx 0 18rpx; }
.hero .kv3 { margin-top: 6rpx; }
.foot {
  display: flex; justify-content: space-between;
  margin-top: 6rpx; padding: 16rpx 28rpx 0; border-top: 2rpx solid #f0f2f7;
  font-size: 24rpx; color: #8a919f;
}
.bar { margin-bottom: 4rpx; }
</style>
