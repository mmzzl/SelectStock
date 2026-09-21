<!-- uniapp/src/pages/holdings/index.vue -->
<script setup>
import { ref } from "vue";
import { onShow } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";
import { toHoldingRow } from "../../logic/view.js";

const rows = ref([]); const loading = ref(false); const err = ref("");
const { holdings } = useClient();
onShow(async () => {
  loading.value = true; err.value = "";
  try {
    const data = await holdings.list({});
    rows.value = (data.items || data || []).map(toHoldingRow);
  } catch (e) { err.value = e.message; } finally { loading.value = false; }
});
const goEdit = (r) => uni.navigateTo({ url: `/pages/holdings/edit?code=${r?.code || ""}` });
const goImport = () => uni.navigateTo({ url: "/pages/holdings/import" });
const goProfit = () => uni.navigateTo({ url: "/pages/profit/index" });
const goTrades = () => uni.navigateTo({ url: "/pages/trades/index" });
</script>
<template>
  <view class="p">
    <view class="bar">
      <button size="mini" @click="goImport">交割单导入</button>
      <button size="mini" @click="goEdit()">新增持仓</button>
      <button size="mini" @click="goProfit">利润分析</button>
      <button size="mini" @click="goTrades">交易记录</button>
    </view>
    <text v-if="err" class="err">{{ err }}</text>
    <view v-for="r in rows" :key="r.code" class="card" @click="goEdit(r)">
      <text>{{ r.name }}（{{ r.code }}）</text>
      <text>建议：{{ r.actionText }}</text>
      <text v-if="r.latestPrice !== null">现价 {{ r.latestPrice }} · 收益 {{ r.pnl }}（{{ r.pnlPct }}%）</text>
      <text class="reason">{{ r.actionReason }}</text>
    </view>
    <view v-if="!rows.length && !loading" class="empty">暂无持仓，点击「交割单导入」或「新增持仓」</view>
  </view>
</template>
