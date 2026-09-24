<!-- uniapp/src/pages/selection/history.vue -->
<script setup>
import { ref } from "vue";
import { onShow } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";
import { toSelectionJobRow, toSelectionResultRow, toStrategyOption, fmtTime } from "../../logic/view.js";

const runs = ref([]); const expanded = ref(null); const detailResults = ref([]);
const err = ref("");
const labels = ref({});        // 策略名 → 中文短标签，来自插件 /strategy-detail
const { selection } = useClient();
onShow(async () => {
  err.value = "";
  try {
    const data = await selection.runs({ page: 1, size: 50 });
    runs.value = (data.items || []).map(toSelectionJobRow);
  } catch (e) { err.value = e.message; }
  if (!Object.keys(labels.value).length) {
    try {
      labels.value = Object.fromEntries(
        (await selection.strategyDetail() || []).map((d) => { const o = toStrategyOption(d); return [o.name, o.label]; }));
    } catch (e) { /* 插件不可用时退回英文策略名 */ }
  }
});
const labelOf = (s) => labels.value[s] || s || "信号";
const fixed2 = (v) => (v === null || v === undefined || Number.isNaN(Number(v)) ? "" : Number(v).toFixed(2));
const ST_TEXT = { done: "完成", running: "进行中", queued: "排队中", failed: "失败" };
async function toggle(j) {
  expanded.value = expanded.value === j.jobId ? null : j.jobId;
  if (expanded.value) {
    try {
      const d = await selection.runDetail(j.jobId);
      detailResults.value = (d.results || []).map(toSelectionResultRow);
    } catch (e) { err.value = e.message; }
  }
}
</script>
<template>
  <view class="p">
    <text v-if="err" class="err">{{ err }}</text>
    <view v-for="j in runs" :key="j.jobId" class="card" @click="toggle(j)">
      <view class="row1">
        <text>{{ (j.strategies || []).map(labelOf).join(" / ") || "全部策略" }}</text>
        <text class="st" :class="j.status"><text class="dot" />{{ ST_TEXT[j.status] || j.status }}</text>
      </view>
      <view class="row1" style="margin-top:4rpx">
        <text class="time">{{ fmtTime(j.createdAt) }}</text>
        <text class="reason">{{ j.resultCount }} 条结果</text>
      </view>
      <view v-if="expanded === j.jobId">
        <view v-for="r in detailResults" :key="r.code" class="sub">
          <text class="badge sig">{{ labelOf(r.strategy) }}</text>
          <text>{{ r.rank ? `#${r.rank} ` : "" }}{{ r.name }}（{{ r.code }}）</text>
          <text class="time">{{ r.price ? `基准价 ${fixed2(r.price)}` : "" }}{{ r.asof ? ` · ${r.asof}` : "" }}</text>
        </view>
        <view v-if="!detailResults.length" class="reason" style="padding:10rpx 0">暂无结果明细</view>
      </view>
    </view>
    <view v-if="!runs.length" class="empty">暂无选股记录</view>
  </view>
</template>
<style>
.sub {
  display: flex; align-items: center; gap: 16rpx;
  border-top: 2rpx solid #f0f2f7; padding: 16rpx 0 4rpx;
}
.sub text { margin: 0; }
</style>
