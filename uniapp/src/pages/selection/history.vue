<!-- uniapp/src/pages/selection/history.vue -->
<script setup>
import { ref } from "vue";
import { onShow } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";
import { toSelectionJobRow } from "../../logic/view.js";

const runs = ref([]); const expanded = ref(null); const detailResults = ref([]);
const err = ref("");
const { selection } = useClient();
onShow(async () => {
  err.value = "";
  try {
    const data = await selection.runs({ page: 1, size: 50 });
    runs.value = (data.items || []).map(toSelectionJobRow);
  } catch (e) { err.value = e.message; }
});
async function toggle(j) {
  expanded.value = expanded.value === j.jobId ? null : j.jobId;
  if (expanded.value) {
    try {
      const d = await selection.runDetail(j.jobId);
      detailResults.value = (d.results || []).map((r) => r);
    } catch (e) { err.value = e.message; }
  }
}
</script>
<template>
  <view class="p">
    <text v-if="err" class="err">{{ err }}</text>
    <view v-for="j in runs" :key="j.jobId" class="card" @click="toggle(j)">
      <text>{{ j.createdAt || '' }} · {{ j.status }} · {{ j.strategies.join('/') }} · {{ j.resultCount }} 条</text>
      <view v-if="expanded === j.jobId">
        <view v-for="r in detailResults" :key="r.code">{{ r.name }}（{{ r.code }}）{{ r.signal }}</view>
      </view>
    </view>
    <view v-if="!runs.length" class="empty">暂无选股记录</view>
  </view>
</template>
