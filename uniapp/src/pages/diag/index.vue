<!-- uniapp/src/pages/diag/index.vue -->
<script setup>
import { ref } from "vue";
import { onShow } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";
import { toDiagRow } from "../../logic/view.js";
import { loadSession } from "../../store/session.js";

const rows = ref([]); const err = ref("");
const { client } = useClient();
onShow(async () => {
  err.value = "";
  const me = loadSession().me || {};
  try {
    const data = await client.get("/api/v1/portfolio/runs", { query: { user_id: me.id } });
    rows.value = (data.items || data || []).map(toDiagRow);
  } catch (e) { err.value = e.message; }
});
const open = (r) => uni.navigateTo({ url: `/pages/diag/detail?runId=${r.runId}` });
</script>
<template>
  <view class="p">
    <text v-if="err" class="err">{{ err }}</text>
    <view v-for="r in rows" :key="r.runId" class="card" @click="open(r)">
      <text>{{ r.day }} · {{ r.status }}</text>
      <text>持仓 {{ r.holdingCount }} · 建议 {{ r.actionCount }} · 总收益 {{ r.totalPnl }}</text>
    </view>
    <view v-if="!rows.length" class="empty">暂无诊断记录（由定时任务产生）</view>
  </view>
</template>
