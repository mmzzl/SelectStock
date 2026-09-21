<!-- uniapp/src/pages/trades/index.vue -->
<script setup>
import { ref } from "vue";
import { onShow } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";
import { toTradeRow } from "../../logic/view.js";

const rows = ref([]); const err = ref("");
const filter = ref({ code: "", direction: "" });
const showAdd = ref(false);
const frm = ref({ trade_date: new Date().toISOString().slice(0, 10), stock_code: "", stock_name: "",
                  direction: "buy", price: "", quantity: "", commission: "0", stamp_tax: "0", transfer_fee: "0" });
const { trades } = useClient();

async function reload() {
  err.value = "";
  const q = {};
  if (filter.value.code) q.code = filter.value.code;
  if (filter.value.direction) q.direction = filter.value.direction;
  try {
    const data = await trades.list(q);
    rows.value = (data.items || []).map(toTradeRow);
  } catch (e) { err.value = e.message; }
}
onShow(reload);

function pickDirection(e) { frm.value.direction = ["buy", "sell"][Number(e.detail.value)]; }
function pickFilterDir(e) {
  filter.value.direction = ["", "buy", "sell"][Number(e.detail.value)];
  reload();
}

async function doAdd() {
  err.value = "";
  const body = { ...frm.value, price: Number(frm.value.price), quantity: Number(frm.value.quantity),
                 amount: 0, commission: Number(frm.value.commission),
                 stamp_tax: Number(frm.value.stamp_tax), transfer_fee: Number(frm.value.transfer_fee) };
  try {
    await trades.create(body);
    showAdd.value = false; reload();
    uni.showToast({ title: "已录入并更新持仓" });
  } catch (e) { err.value = e.message; }
}
async function del(r) {
  if (r.source !== "manual") { uni.showToast({ title: "导入记录不可单笔删除", icon: "none" }); return; }
  uni.showModal({ title: "删除记录", content: `删除 ${r.name} ${r.directionText} ${r.quantity}？`,
    success: async (o) => { if (o.confirm) { try { await trades.remove(r.id); reload(); } catch (e) { err.value = e.message; } } } });
}
</script>
<template>
  <view class="p">
    <view class="bar">
      <input v-model="filter.code" placeholder="代码" style="width:200rpx" @confirm="reload" />
      <picker :range="['全部', '买入', '卖出']" @change="pickFilterDir">
        <text>{{ filter.direction === 'sell' ? '卖出' : filter.direction === 'buy' ? '买入' : '方向' }}</text>
      </picker>
      <button size="mini" @click="reload">查询</button>
      <button size="mini" @click="showAdd = !showAdd">手动录入</button>
    </view>
    <text v-if="err" class="err">{{ err }}</text>
    <view v-if="showAdd" class="form">
      <input v-model="frm.stock_code" placeholder="6位代码" />
      <input v-model="frm.stock_name" placeholder="名称" />
      <picker :range="['买入', '卖出']" :value="frm.direction === 'sell' ? 1 : 0" @change="pickDirection">
        <text>{{ frm.direction === 'sell' ? '卖出' : '买入' }}</text>
      </picker>
      <input v-model="frm.price" type="digit" placeholder="价格" />
      <input v-model="frm.quantity" type="number" placeholder="数量" />
      <input v-model="frm.trade_date" placeholder="YYYY-MM-DD" />
      <input v-model="frm.commission" type="digit" placeholder="手续费" />
      <input v-model="frm.stamp_tax" type="digit" placeholder="印花税" />
      <input v-model="frm.transfer_fee" type="digit" placeholder="过户费" />
      <button :disabled="!frm.stock_code || !frm.price || !frm.quantity" @click="doAdd">提交</button>
    </view>
    <view v-for="r in rows" :key="r.id" class="card">
      <text>{{ r.date }} {{ r.name }}（{{ r.code }}）</text>
      <text>{{ r.directionText }} {{ r.quantity }} @ {{ r.price }} · 结算 {{ r.settledAmount }}</text>
      <button size="mini" @click="del(r)">删除</button>
    </view>
    <view v-if="!rows.length" class="empty">暂无交易记录</view>
  </view>
</template>
