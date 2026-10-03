<!-- uniapp/src/pages/trades/index.vue —— 交割记录：按成交日分组，只做展示与录入（R1-3） -->
<script setup>
import { ref, computed } from "vue";
import { onShow } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";
import { toTradeRow } from "../../logic/view.js";

const rows = ref([]); const err = ref(""); const loading = ref(false);
const filter = ref({ code: "", direction: "" });
const showAdd = ref(false);
const frm = ref({ trade_date: new Date().toISOString().slice(0, 10), stock_code: "", stock_name: "",
                  direction: "buy", price: "", quantity: "", commission: "0", stamp_tax: "0", transfer_fee: "0" });
const { trades } = useClient();

async function reload() {
  loading.value = true; err.value = "";
  const q = {};
  if (filter.value.code) q.code = filter.value.code;
  if (filter.value.direction) q.direction = filter.value.direction;
  try {
    const data = await trades.list(q);
    rows.value = (data.items || []).map(toTradeRow);
  } catch (e) { err.value = e.message; } finally { loading.value = false; }
}
onShow(reload);

const days = computed(() => {
  const out = [];
  for (const r of rows.value) {
    let g = out[out.length - 1];
    if (!g || g.day !== r.date) { g = { day: r.date, items: [], buy: 0, sell: 0 }; out.push(g); }
    g.items.push(r);
    if (r.direction === "sell") g.sell += 1; else g.buy += 1;   // 只计数，不加总金额
  }
  return out;
});

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
  uni.showModal({ title: "删除记录", content: `${r.date} ${r.name} ${r.directionText} ${r.quantity}？`,
    success: async (o) => { if (o.confirm) { try { await trades.remove(r.id); reload(); } catch (e) { err.value = e.message; } } } });
}
const money = (x) => (x === null || x === undefined ? "—" : Number(x).toFixed(2));
</script>
<template>
  <view class="p">
    <view class="bar">
      <input v-model="filter.code" placeholder="代码" style="width:200rpx" @confirm="reload" />
      <picker :range="['全部', '买入', '卖出']" @change="pickFilterDir">
        <text>{{ filter.direction === 'sell' ? '卖出' : filter.direction === 'buy' ? '买入' : '方向' }}</text>
      </picker>
      <button size="mini" @click="reload">查询</button>
      <button size="mini" @click="showAdd = !showAdd">{{ showAdd ? '收起' : '手动录入' }}</button>
    </view>
    <text v-if="err" class="err">{{ err }}</text>

    <view v-if="showAdd" class="form">
      <view class="sec">手动录入一笔</view>
      <view class="two">
        <input v-model="frm.stock_code" placeholder="6位代码" />
        <input v-model="frm.stock_name" placeholder="名称（可空）" />
      </view>
      <view class="two">
        <picker :range="['买入', '卖出']" :value="frm.direction === 'sell' ? 1 : 0" @change="pickDirection">
          <text>{{ frm.direction === 'sell' ? '卖出' : '买入' }}</text>
        </picker>
        <input v-model="frm.trade_date" placeholder="YYYY-MM-DD" />
      </view>
      <view class="two">
        <input v-model="frm.price" type="digit" placeholder="价格" />
        <input v-model="frm.quantity" type="number" placeholder="数量" />
      </view>
      <view class="two">
        <input v-model="frm.commission" type="digit" placeholder="手续费" />
        <input v-model="frm.stamp_tax" type="digit" placeholder="印花税" />
      </view>
      <input v-model="frm.transfer_fee" type="digit" placeholder="过户费" />
      <button class="btn-big" :disabled="!frm.stock_code || !frm.price || !frm.quantity" @click="doAdd">提交</button>
    </view>

    <view class="head">{{ loading ? "加载中…" : `共 ${rows.length} 笔` }}</view>
    <block v-for="g in days" :key="g.day">
      <view class="grp">
        <text class="t">{{ g.day }}</text>
        <text class="c">{{ g.items.length }} 笔 · 买 {{ g.buy }} 卖 {{ g.sell }}</text>
      </view>
      <view v-for="r in g.items" :key="r.id" class="trow">
        <view class="l">
          <text class="n">{{ r.name }}（{{ r.code }}）</text>
          <text class="sub">{{ r.quantity }} 股 @ {{ money(r.price) }} · 结算 {{ money(r.settledAmount) }}</text>
        </view>
        <view class="r">
          <text class="badge" :class="r.direction">{{ r.directionText }}</text>
          <text v-if="r.source === 'manual'" class="del" @click.stop="del(r)">删除</text>
          <text v-else class="q">导入</text>
        </view>
      </view>
    </block>
    <view v-if="!rows.length && !loading" class="empty">暂无交易记录</view>
  </view>
</template>
<style scoped>
.head { font-size: 24rpx; color: #8a919f; margin: 22rpx 4rpx 0; }
.two { display: flex; gap: 16rpx; }
.two input, .two picker { flex: 1; }
.del { font-size: 24rpx; color: #e64545; margin-top: 8rpx; padding: 4rpx 10rpx; }
</style>
