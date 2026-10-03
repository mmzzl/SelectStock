<!-- uniapp/src/pages/holdings/edit.vue —— 新增/编辑持仓：带 code 参数即为编辑，回填并走 PUT -->
<script setup>
import { ref, computed } from "vue";
import { onLoad } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";
import { toHoldingRow } from "../../logic/view.js";

const id = ref("");
const code = ref("");
const name = ref("");
const quantity = ref("");
const costPrice = ref("");
const enabled = ref(true);
const snapshot = ref(null);   // 只读：诊断插件下发的现价/建议
const derived = ref(false);   // 该行是否由交割单推导
const loading = ref(false);
const saving = ref(false);
const err = ref("");

const { holdings } = useClient();
const editing = computed(() => Boolean(id.value));
const canSave = computed(() =>
  (editing.value || /^\d{6}$/.test(code.value.trim())) &&
  quantity.value !== "" && Number.isFinite(Number(quantity.value)) && Number(quantity.value) >= 0 &&
  costPrice.value !== "" && Number.isFinite(Number(costPrice.value)) && Number(costPrice.value) >= 0);

onLoad(async (q) => {
  if (!q.code) return;
  code.value = q.code;
  loading.value = true;
  // 列表接口不返回单条，按 code 在持仓里定位 id 才能走 PUT
  try {
    const data = await holdings.list({});
    const raw = (data.items || data || []).find((h) => h.code === q.code);
    if (!raw) { err.value = `未找到 ${q.code} 的持仓，请重新添加`; return; }
    id.value = raw.id;
    name.value = raw.name || "";
    quantity.value = String(raw.quantity ?? "");
    costPrice.value = String(raw.cost_price ?? "");
    enabled.value = raw.enabled !== false;
    derived.value = Boolean(raw.derived_from_trades);
    snapshot.value = toHoldingRow(raw);
  } catch (e) { err.value = e.message; } finally { loading.value = false; }
});

async function save() {
  if (!canSave.value || saving.value) return;
  err.value = ""; saving.value = true;
  const body = {
    name: name.value.trim(),
    quantity: Number(quantity.value),
    cost_price: Number(costPrice.value),
    enabled: enabled.value,
  };
  try {
    if (editing.value) await holdings.update(id.value, body);
    else await holdings.create({ ...body, code: code.value.trim() });
    uni.navigateBack();
  } catch (e) { err.value = e.message; } finally { saving.value = false; }
}

const money = (v) => (v === null || v === undefined ? "—" : Number(v).toFixed(2));
const pnlCls = (v) => (v === null || v === undefined || v === 0 ? "" : v > 0 ? "up" : "down");
</script>
<template>
  <view class="p">
    <view class="sec">{{ editing ? "编辑持仓" : "新增持仓" }}</view>

    <view class="form">
      <view class="two">
        <view class="fld">
          <text class="lab">代码</text>
          <input v-model="code" type="number" maxlength="6" placeholder="6位数字" :disabled="editing" />
        </view>
        <view class="fld">
          <text class="lab">名称</text>
          <input v-model="name" maxlength="16" placeholder="可留空" />
        </view>
        <view class="fld">
          <text class="lab">持仓数量（股）</text>
          <input v-model="quantity" type="number" placeholder="0 表示已清仓" />
        </view>
        <view class="fld">
          <text class="lab">成本价</text>
          <input v-model="costPrice" type="digit" placeholder="如 16.80" />
        </view>
      </view>
      <view class="switch">
        <text class="lab">纳入诊断</text>
        <switch :checked="enabled" color="#2b5cff" @change="enabled = $event.detail.value" />
      </view>
      <text v-if="derived" class="tip">
        该持仓由交割单推导，数量与成本会在下次导入时被覆盖
      </text>
    </view>

    <view v-if="snapshot" class="card ro">
      <view class="row1">
        <text class="rt">最近诊断</text>
        <text class="badge" :class="snapshot.action || 'none'">{{ snapshot.actionText }}</text>
      </view>
      <view class="kv3">
        <view class="k"><text class="n">{{ money(snapshot.latestPrice) }}</text><text class="l">现价</text></view>
        <view class="k">
          <text class="n" :class="pnlCls(snapshot.pnl)">{{ money(snapshot.pnl) }}</text><text class="l">浮动盈亏</text>
        </view>
        <view class="k">
          <text class="n" :class="pnlCls(snapshot.pnlPct)">{{ snapshot.pnlPct === null ? "—" : snapshot.pnlPct + "%" }}</text>
          <text class="l">收益率</text>
        </view>
      </view>
      <text v-if="snapshot.actionReason" class="reason">{{ snapshot.actionReason }}</text>
    </view>

    <text v-if="err" class="err">{{ err }}</text>
    <button class="btn-big" :disabled="!canSave || saving || loading" @click="save">
      {{ saving ? "保存中…" : editing ? "保存修改" : "添加持仓" }}
    </button>
  </view>
</template>
<style scoped>
.two { display: flex; flex-wrap: wrap; gap: 0 20rpx; }
.fld { flex: 1 1 45%; min-width: 260rpx; }
.lab { display: block; font-size: 22rpx; color: #8a919f; margin-top: 8rpx; }
.switch { display: flex; align-items: center; justify-content: space-between; margin-top: 12rpx; }
.switch .lab { margin: 0; }
.tip { display: block; font-size: 22rpx; color: #ff9f2e; margin-top: 12rpx; }
.ro .rt { font-size: 28rpx; font-weight: 600; }
.ro .reason { margin-top: 10rpx; }
</style>
