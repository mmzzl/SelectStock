<!-- uniapp/src/pages/diag/detail.vue —— 明细字段全部来自服务端 -->
<script setup>
import { ref } from "vue";
import { onLoad } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";
import { toHoldingRow } from "../../logic/view.js";

const results = ref([]); const err = ref("");
const { client } = useClient();
onLoad(async (q) => {
  try {
    const data = await client.get(`/api/portfolio/diag/${q.runId}`);
    results.value = (data.results || []).map(toHoldingRow);
  } catch (e) { err.value = e.message; }
});
const pnlCls = (v) => (v === null ? "" : v > 0 ? "up" : v < 0 ? "down" : "");
</script>
<template>
  <view class="p">
    <text v-if="err" class="err">{{ err }}</text>
    <view class="sec" style="margin-top:8rpx">诊断明细（{{ results.length }} 只）</view>
    <view v-for="r in results" :key="r.code" class="card">
      <view class="row1">
        <text>{{ r.name }}（{{ r.code }}）</text>
        <text class="badge" :class="r.action || 'none'">{{ r.actionText }}</text>
      </view>
      <text>现价 {{ r.latestPrice }} ·
        <text :class="pnlCls(r.pnl)">盈亏 {{ r.pnl }}（{{ r.pnlPct }}%）</text>
      </text>
      <text class="reason">{{ r.actionReason }}</text>
    </view>
    <view v-if="!results.length && !err" class="empty">暂无明细</view>
  </view>
</template>
