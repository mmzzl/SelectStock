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
    const data = await client.get("/api/portfolio/runs", { query: { user_id: me.id } });
    rows.value = (data.items || data || []).map(toDiagRow);
  } catch (e) { err.value = e.message; }
});
const pnlCls = (v) => (v === null ? "" : v > 0 ? "up" : v < 0 ? "down" : "");
const ST_TEXT = { completed: "已完成", running: "进行中", queued: "排队中", failed: "失败" };
const open = (r) => uni.navigateTo({ url: `/pages/diag/detail?runId=${r.runId}` });
</script>
<template>
  <view>
    <view class="band">
      <view class="band-in">
        <view>
          <view class="bt">持仓诊断</view>
          <view class="bs">每日盘后自动生成 · 结论来自诊断插件</view>
        </view>
        <text class="bt">{{ rows.length }}</text>
      </view>
    </view>

    <view class="p">
      <text v-if="err" class="err">{{ err }}</text>
      <view v-for="r in rows" :key="r.runId" class="card" @click="open(r)">
        <view class="row1">
          <text>{{ r.day }}</text>
          <text class="st" :class="r.status">
            <text class="dot" />{{ ST_TEXT[r.status] || r.status }}
          </text>
        </view>
        <view class="kv3">
          <view class="k"><text class="n">{{ r.holdingCount }}</text><text class="l">诊断持仓</text></view>
          <view class="k"><text class="n">{{ r.actionCount }}</text><text class="l">操作建议</text></view>
          <view class="k"><text class="n" :class="pnlCls(r.totalPnl)">{{ r.totalPnl }}</text><text class="l">总收益</text></view>
        </view>
      </view>
      <view v-if="!rows.length && !err" class="empty">
        <view>暂无诊断记录</view>
        <view class="reason">由每日 18:00 定时任务自动生成</view>
      </view>
    </view>
  </view>
</template>
