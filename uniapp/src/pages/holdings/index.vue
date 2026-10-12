<!-- uniapp/src/pages/holdings/index.vue —— 持仓总览：按诊断建议分组；数值一律后端/插件下发（R1-3） -->
<script setup>
import { ref, computed } from "vue";
import { onShow } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";
import { sellTriggerText, toHoldingRow, toPnlView } from "../../logic/view.js";

const rows = ref([]); const loading = ref(false); const err = ref("");
const summary = ref(null);           // 来自 /holdings/pnl 的 summary，前端不做加总
const { holdings } = useClient();
const today = () => new Date().toISOString().slice(0, 10);

onShow(async () => {
  loading.value = true; err.value = "";
  try {
    const data = await holdings.list({});
    rows.value = (data.items || data || []).map(toHoldingRow);
    // 汇总条与列表分开取：插件不可用时只降级掉汇总，不影响持仓列表
    try { summary.value = toPnlView(await holdings.pnl({ natural_day: today() })); } catch { summary.value = null; }
  } catch (e) { err.value = e.message; } finally { loading.value = false; }
});

const GROUPS = [
  { key: "sell", label: "建议卖出" },
  { key: "add", label: "建议加仓" },
  { key: "hold", label: "建议持有" },
  { key: "none", label: "暂无建议" },
];
const grouped = computed(() => {
  // 诊断快照的 holding_pnl 只有当前仓位的浮动；买过又卖过一部分的股票，卖出那部分的
  // 已实现盈亏在插件 per_code 里，这里按代码取过来一起显示（前端不加总）。
  const byCode = new Map((summary.value?.rows || []).map((x) => [x.code, x]));
  const withPnl = rows.value.map((r) => ({ ...r, agg: byCode.get(r.code) || null }));
  const pick = (g) => withPnl.filter((r) => (g.key === "none" ? !r.action : r.action === g.key));
  return GROUPS.map((g) => ({ ...g, items: pick(g) })).filter((g) => g.items.length);
});

const pnlCls = (v) => (v === null || v === 0 ? "" : v > 0 ? "up" : "down");
const money = (v) => (v === null || v === undefined ? "—" : Number(v).toFixed(2));
const signed = (v) => (v === null || v === undefined ? "—" : (v > 0 ? "+" : "") + money(v));
const goEdit = (r) => uni.navigateTo({ url: `/pages/holdings/edit${r ? `?code=${encodeURIComponent(r.code)}` : ""}` });
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
          <view class="bs">共 {{ rows.length }} 只 · 建议与现价由诊断插件返回</view>
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
      <view v-if="summary" class="card hero">
        <view class="kv3">
          <view class="k">
            <text class="n" :class="pnlCls(summary.unrealized.totalUnrealized)">{{ money(summary.unrealized.totalUnrealized) }}</text>
            <text class="l">未实现盈亏</text>
          </view>
          <view class="k">
            <text class="n">{{ money(summary.unrealized.totalValue) }}</text>
            <text class="l">持仓市值</text>
          </view>
          <view class="k">
            <text class="n" :class="pnlCls(summary.realized.totalRealized)">{{ money(summary.realized.totalRealized) }}</text>
            <text class="l">已实现盈亏</text>
          </view>
        </view>
      </view>

      <text v-if="err" class="err">{{ err }}</text>

      <block v-for="g in grouped" :key="g.key">
        <view class="grp">
          <text class="t">{{ g.label }}</text>
          <text class="c">{{ g.items.length }} 只</text>
        </view>
        <view v-for="r in g.items" :key="r.code" class="trow" @click="goEdit(r)">
          <view class="l">
            <text class="d">现价 {{ r.latestPrice ?? "—" }} · 成本 {{ r.cost ?? "—" }} · {{ r.shares ?? "—" }} 股</text>
            <text class="n">{{ r.name }}（{{ r.code }}）</text>
            <text v-if="r.actionReason" class="sub">{{ r.actionReason }}</text>
            <text v-if="r.agg && r.agg.realizedPnl" class="sub acc">已实现 {{ signed(r.agg.realizedPnl) }} · 含已实现合计 {{ signed(r.agg.totalPnl) }}</text>
            <text v-if="sellTriggerText(r)" class="sub trig">{{ sellTriggerText(r) }}</text>
          </view>
          <view class="r">
            <text class="badge" :class="r.action || 'none'">{{ r.actionText }}</text>
            <text class="amt" :class="pnlCls(r.pnl)">{{ r.pnl === null ? "—" : money(r.pnl) }}</text>
            <text v-if="r.pnlPct !== null" class="q" :class="pnlCls(r.pnlPct)">{{ r.pnlPct > 0 ? "+" : "" }}{{ r.pnlPct }}%</text>
          </view>
        </view>
      </block>

      <view v-if="!rows.length && !loading" class="empty">
        <view>暂无持仓</view>
        <view class="reason">可通过「交割单导入」或「新增持仓」开始</view>
      </view>
    </view>
  </view>
</template>
<style scoped>
.hero { padding: 10rpx 0 18rpx; }
.hero .kv3 { margin-top: 6rpx; }
.trow .trig { color: #e64545; }
.trow .acc { color: #2b5cff; }
</style>
