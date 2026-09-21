<!-- uniapp/src/pages/selection/index.vue -->
<script setup>
import { ref } from "vue";
import { onShow, onUnload } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";
import { toSelectionResultRow } from "../../logic/view.js";

const { selection } = useClient();
const strategies = ref([]);
const chosen = ref([]);
const job = ref(null);          // { jobId, status, results, ... }
const results = ref([]);
const polling = ref(false);
const err = ref("");
let timer = null;

onShow(async () => {
  if (!strategies.value.length) {
    try { strategies.value = await selection.strategies(); } catch (e) { err.value = e.message || "选股插件不可用"; }
  }
});
onUnload(() => { clearInterval(timer); timer = null; });

async function trigger() {
  err.value = "";
  try {
    const out = await selection.run(chosen.value.length ? chosen.value : null);
    job.value = { jobId: out.job_id, status: out.status, results: [] };
    startPoll(out.job_id);
  } catch (e) { err.value = e.message; }
}
function startPoll(jobId) {
  polling.value = true;
  timer = setInterval(async () => {
    try {
      const d = await selection.runDetail(jobId);
      job.value = { jobId: d.job_id, status: d.status, strategies: d.strategies, results: d.results || [] };
      results.value = (d.results || []).map(toSelectionResultRow);
      if (d.status === "done" || d.status === "failed") { clearInterval(timer); timer = null; polling.value = false; }
    } catch (e) { clearInterval(timer); timer = null; polling.value = false; err.value = e.message; }
  }, 2000);
}
const goHistory = () => uni.navigateTo({ url: "/pages/selection/history" });
</script>
<template>
  <view class="p">
    <view v-for="s in strategies" :key="s" class="chip" :class="{ on: chosen.includes(s) }"
          @click="chosen = chosen.includes(s) ? chosen.filter(x => x !== s) : [...chosen, s]">{{ s }}</view>
    <button :disabled="polling" @click="trigger">{{ polling ? "选股中…" : "开始选股" }}</button>
    <button size="mini" @click="goHistory">历史回看</button>
    <text v-if="err" class="err">{{ err }}</text>
    <view v-for="r in results" :key="r.code" class="card">
      <text>{{ r.name }}（{{ r.code }}）信号：{{ r.signal }}</text>
      <text class="reason">{{ r.reason }}</text>
    </view>
    <view v-if="job && !results.length && job.status !== 'done'" class="empty">任务 {{ job.status }}…</view>
  </view>
</template>
