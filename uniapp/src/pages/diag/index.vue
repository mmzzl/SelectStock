<!-- uniapp/src/pages/diag/index.vue —— 诊断历史 + 手动触发；结论全部来自插件，前端只展示 -->
<script setup>
import { ref } from "vue";
import { onShow, onHide, onUnload } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";
import { toDiagRow } from "../../logic/view.js";

const rows = ref([]); const err = ref(""); const loading = ref(false);
const triggering = ref(false);
const { diag } = useClient();
const PENDING = ["queued", "running"];
const ST_TEXT = { completed: "已完成", running: "进行中", queued: "排队中", failed: "失败" };
const COOLDOWN_MIN = 10;   // 与后端 diag_service._REFRESH_COOLDOWN_MINUTES 保持一致

let timer = null;
function stopPolling() { clearTimeout(timer); timer = null; }

// 后端时间戳是 naive UTC，补个 Z 才是 UTC，否则本机 UTC+8 下会以为刚跑完
function waitLeft() {
  const raw = rows.value[0]?.updatedAt;
  if (!raw) return 0;
  const s = String(raw).trim().replace(" ", "T");
  const ts = Date.parse(/[zZ]$|[+-]\d{2}:?\d{2}$/.test(s) ? s : s + "Z");
  if (Number.isNaN(ts)) return 0;
  return Math.max(0, Math.ceil((COOLDOWN_MIN * 60e3 - (Date.now() - ts)) / 60e3));
}

async function load() {
  loading.value = true; err.value = "";
  try {
    const data = await diag.runs({ size: 50 });
    rows.value = (data.items || data || []).map(toDiagRow);
    // 诊断是后台任务：还有未结束的 run 就 3 秒轮一次，落定即停
    stopPolling();
    if (rows.value.some((r) => PENDING.includes(r.status))) timer = setTimeout(load, 3000);
    else if (waitLeft() > 0) timer = setTimeout(load, 60000);   // 冷却期按分钟倒数，好让人知道何时能刷新
  } catch (e) { err.value = e.message; } finally { loading.value = false; }
}

async function runNow() {
  if (triggering.value) return;
  err.value = ""; triggering.value = true;
  try {
    const out = await diag.run();
    uni.showToast({
      title: PENDING.includes(out?.status) ? "诊断已启动" : `结果未到 10 分钟，${waitLeft()} 分钟后可刷新`,
      icon: "none",
    });
    await load();
  } catch (e) { err.value = e.message; } finally { triggering.value = false; }
}

onShow(load);
onHide(stopPolling);
onUnload(stopPolling);

const pnlCls = (v) => (v === null || v === 0 ? "" : v > 0 ? "up" : "down");
const open = (r) => uni.navigateTo({ url: `/pages/diag/detail?runId=${r.runId}` });
</script>
<template>
  <view>
    <view class="band">
      <view class="band-in">
        <view>
          <view class="bt">持仓诊断</view>
          <view class="bs">{{ waitLeft() > 0 ? `每日 18:00 自动跑 · ${waitLeft()} 分钟后可刷新` : "每日 18:00 自动跑 · 可随时刷新" }}</view>
        </view>
        <button class="band-btn" :disabled="triggering" @click="runNow">
          {{ triggering ? "触发中…" : "刷新诊断" }}
        </button>
      </view>
    </view>

    <view class="p">
      <text v-if="err" class="err">{{ err }}</text>

      <view v-for="r in rows" :key="r.runId" class="trow" @click="open(r)">
        <view class="l">
          <text class="d">{{ r.day }}</text>
          <text class="n">{{ r.holdingCount }} 只持仓 · {{ r.actionCount }} 条建议</text>
        </view>
        <view class="r">
          <text class="st" :class="r.status"><text class="dot" />{{ ST_TEXT[r.status] || r.status }}</text>
          <text class="amt" :class="pnlCls(r.totalPnl)">{{ r.totalPnl }}</text>
        </view>
      </view>

      <view v-if="!rows.length && !loading" class="empty">
        <view>暂无诊断记录</view>
        <view class="reason">点右上角「刷新诊断」跑一次，或等每日 18:00 自动任务</view>
      </view>
    </view>
  </view>
</template>
<style scoped>
.trow .r .st { display: inline-flex; justify-content: flex-end; }
.trow .r .amt { margin-top: 6rpx; }
</style>
