<!-- uniapp/src/pages/diag/detail.vue —— 单次诊断明细：字段全部来自服务端 -->
<script setup>
import { ref } from "vue";
import { onLoad } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";
import { toHoldingRow } from "../../logic/view.js";

const run = ref(null); const results = ref([]); const err = ref("");
const { diag } = useClient();
onLoad(async (q) => {
  try {
    const data = await diag.detail(q.runId);
    run.value = data;
    results.value = (data.results || []).map(toHoldingRow);
  } catch (e) { err.value = e.message; }
});

const money = (v) => (v === null || v === undefined ? "—" : Number(v).toFixed(2));
const pnlCls = (v) => (v === null || v === undefined || v === 0 ? "" : v > 0 ? "up" : "down");
</script>
<template>
  <view class="p">
    <text v-if="err" class="err">{{ err }}</text>

    <view v-if="run" class="card hero">
      <view class="row1">
        <text class="rt">{{ run.natural_day }} 诊断</text>
        <text class="badge" :class="run.status === 'completed' ? 'hold' : 'none'">{{ run.status }}</text>
      </view>
      <view class="kv3">
        <view class="k"><text class="n">{{ run.held_code_count }}</text><text class="l">诊断持仓</text></view>
        <view class="k"><text class="n">{{ run.action_count }}</text><text class="l">操作建议</text></view>
        <view class="k">
          <text class="n" :class="pnlCls(run.total_pnl)">{{ money(run.total_pnl) }}</text><text class="l">总收益</text>
        </view>
      </view>
      <text v-if="run.error" class="reason">失败原因：{{ run.error }}</text>
    </view>

    <view v-for="r in results" :key="r.code" class="trow">
      <view class="l">
        <text class="d">现价 {{ money(r.latestPrice) }} · {{ r.shares ?? "—" }} 股</text>
        <text class="n">{{ r.name }}（{{ r.code }}）</text>
        <text v-if="r.actionReason" class="sub">{{ r.actionReason }}</text>
      </view>
      <view class="r">
        <text class="badge" :class="r.action || 'none'">{{ r.actionText }}</text>
        <text class="amt" :class="pnlCls(r.pnl)">{{ r.pnl === null ? "—" : money(r.pnl) }}</text>
        <text v-if="r.pnlPct !== null" class="q" :class="pnlCls(r.pnlPct)">
          {{ r.pnlPct > 0 ? "+" : "" }}{{ r.pnlPct }}%
        </text>
      </view>
    </view>

    <view v-if="run && !results.length && !err" class="empty">该次诊断没有明细结果</view>
  </view>
</template>
<style scoped>
.hero { padding: 10rpx 0 18rpx; }
.hero .rt { font-size: 30rpx; font-weight: 600; }
.hero .kv3 { margin-top: 6rpx; }
</style>
