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
    const data = await client.get(`/api/v1/portfolio/diag/${q.runId}`);
    results.value = (data.results || []).map(toHoldingRow);
  } catch (e) { err.value = e.message; }
});
</script>
<template>
  <view class="p">
    <view v-for="r in results" :key="r.code" class="card">
      <text>{{ r.name }}（{{ r.code }}）建议：{{ r.actionText }}</text>
      <text>现价 {{ r.latestPrice }} · 盈亏 {{ r.pnl }}（{{ r.pnlPct }}%）</text>
      <text class="reason">{{ r.actionReason }}</text>
    </view>
    <view v-if="err" class="err">{{ err }}</view>
  </view>
</template>
