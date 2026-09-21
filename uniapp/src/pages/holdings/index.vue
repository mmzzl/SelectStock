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
const pnlCls = (v) => (v === null ? "" : v > 0 ? "up" : v < 0 ? "down" : "");
const goEdit = (r) => uni.navigateTo({ url: `/pages/holdings/edit?code=${r?.code || ""}` });
const goImport = () => uni.navigateTo({ url: "/pages/holdings/import" });
const goProfit = () => uni.navigateTo({ url: "/pages/profit/index" });
const goTrades = () => uni.navigateTo({ url: "/pages/trades/index" });
</script>
<template>
  <view>
    <view class="band">
      <view class="band-in">
        <view>
          <view class="bt">持仓总览</view>
          <view class="bs">共 {{ rows.length }} 只 · 诊断建议由插件返回</view>
        </view>
        <button class="band-btn" @click="goImport">导入交割单</button>
      </view>
    </view>

    <view class="menu-grid">
      <view class="mi" @click="goImport"><view class="ic b1">单</view><view class="mt">交割单导入</view></view>
      <view class="mi" @click="goEdit()"><view class="ic b2">增</view><view class="mt">新增持仓</view></view>
      <view class="mi" @click="goProfit"><view class="ic b3">利</view><view class="mt">利润分析</view></view>
      <view class="mi" @click="goTrades"><view class="ic b4">交</view><view class="mt">交易记录</view></view>
    </view>

    <view class="p">
      <text v-if="err" class="err">{{ err }}</text>
      <view v-for="r in rows" :key="r.code" class="card" @click="goEdit(r)">
        <view class="row1">
          <text>{{ r.name }}（{{ r.code }}）</text>
          <text class="badge" :class="r.action || 'none'">{{ r.actionText }}</text>
        </view>
        <text v-if="r.latestPrice !== null">
          现价 {{ r.latestPrice }} ·
          <text :class="pnlCls(r.pnl)">收益 {{ r.pnl }}（{{ r.pnlPct }}%）</text>
        </text>
        <text class="reason">{{ r.actionReason }}</text>
      </view>
      <view v-if="!rows.length && !loading" class="empty">
        <view>暂无持仓</view>
        <view class="reason">可通过「交割单导入」或「新增持仓」开始</view>
      </view>
    </view>
  </view>
</template>
