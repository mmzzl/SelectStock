<!-- uniapp/src/pages/profit/index.vue —— 纯透传 /holdings/pnl（R1-3） -->
<script setup>
import { ref } from "vue";
import { onShow } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";
import { toPnlView } from "../../logic/view.js";

const v = ref({ rows: [], realized: {}, unrealized: {} });
const today = ref(""); const err = ref("");
const { holdings } = useClient();
onShow(async () => {
  today.value = new Date().toISOString().slice(0, 10);
  err.value = "";
  try {
    const res = await holdings.pnl({ natural_day: today.value });
    v.value = toPnlView(res);
  } catch (e) { err.value = e.message || "插件不可用"; }
});
</script>
<template>
  <view class="p">
    <view v-if="err" class="err">{{ err }}（由持仓诊断插件返回，前端不计算）</view>
    <view class="card">
      <text>未实现盈亏 {{ v.unrealized.totalUnrealized }} · 持仓市值 {{ v.unrealized.totalValue }}</text>
      <text>已实现盈亏 {{ v.realized.totalRealized }} · 费用 {{ v.realized.totalFees }}</text>
    </view>
    <view v-for="r in v.rows" :key="r.code" class="card">
      <text>{{ r.name }}（{{ r.code }}）仓位 {{ r.position }} 成本 {{ r.avgCost }} 现价 {{ r.latestPrice }}</text>
      <text>已实现 {{ r.realizedPnl }} · 浮动 {{ r.unrealizedPnl }} · 合计 {{ r.totalPnl }}</text>
    </view>
    <view v-if="!v.rows.length" class="empty">暂无持仓数据</view>
  </view>
</template>
