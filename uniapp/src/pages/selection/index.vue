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
  <view>
    <view class="band">
      <view class="band-in">
        <view>
          <view class="bt">智能选股</view>
          <view class="bs">选择策略后触发 · 结果由选股插件产出</view>
        </view>
        <button class="band-btn" @click="goHistory">历史回看</button>
      </view>
    </view>

    <view class="p">
      <view class="card">
        <view class="sec" style="margin-top:0">选择策略（不选则全部）</view>
        <view>
          <view v-for="s in strategies" :key="s" class="chip" :class="{ on: chosen.includes(s) }"
                @click="chosen = chosen.includes(s) ? chosen.filter(x => x !== s) : [...chosen, s]">{{ s }}</view>
          <text v-if="!strategies.length" class="reason">策略列表加载中或插件不可用</text>
        </view>
        <button class="btn-big" :disabled="polling" @click="trigger">
          {{ polling ? "选股中，请稍候…" : "开 始 选 股" }}
        </button>
        <view v-if="job" class="row1" style="margin-top:8rpx">
          <text class="st" :class="job.status"><text class="dot" />任务 {{ job.status }}</text>
          <text class="time">{{ job.jobId }}</text>
        </view>
      </view>

      <text v-if="err" class="err">{{ err }}</text>

      <view v-if="results.length" class="sec">选股结果（{{ results.length }} 只）</view>
      <view v-for="(r, i) in results" :key="r.code" class="card">
        <view class="row1">
          <text><text class="rank">{{ i + 1 }}</text> {{ r.name }}（{{ r.code }}）</text>
          <text class="badge sig">{{ r.signal }}</text>
        </view>
        <text class="reason">{{ r.reason }}</text>
      </view>
      <view v-if="job && !results.length && polling" class="empty">全市场扫描中，请稍候…</view>
    </view>
  </view>
</template>
<style>
.rank {
  display: inline-block; width: 40rpx; height: 40rpx; line-height: 40rpx;
  text-align: center; border-radius: 10rpx;
  background: #eef3ff; color: #2b5cff; font-size: 24rpx; font-weight: 700;
}
</style>
