<!-- uniapp/src/pages/sector/board.vue —— 一个格子的详情：板块指标 + 成分股区间表现 -->
<script setup>
import { computed, ref } from "vue";
import { onLoad } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";
import { signedPct, toSectorMemberRow, yiText } from "../../logic/view.js";

const { sector } = useClient();
const WINDOWS = [3, 7, 30];

const code = ref("");
const win = ref(3);
const title = ref("");
const board = ref(null);
const rows = ref([]);
const err = ref("");
const loading = ref(false);

async function load() {
  loading.value = true;
  err.value = "";
  try {
    const d = await sector.board(code.value, win.value);
    board.value = d.board;
    rows.value = (d.members || []).map(toSectorMemberRow);
    if (d.board?.board_name) title.value = d.board.board_name;
    uni.setNavigationBarTitle({ title: d.board?.board_name || title.value || "板块详情" });
  } catch (e) {
    err.value = e.message || "板块数据加载失败";
    rows.value = [];
  } finally {
    loading.value = false;
  }
}

onLoad((q) => {
  code.value = q.code || "";
  win.value = Number(q.window) || 3;
  title.value = decodeURIComponent(q.name || "");
  load();
});

function pickWindow(w) {
  if (w === win.value) return;
  win.value = w;
  load();
}

const pctCls = (v) => (v === null || v === undefined || v === 0 ? "" : v > 0 ? "up" : "down");
const ratioText = computed(() => {
  const r = board.value?.turnover_ratio;
  if (r === null || r === undefined) return "—";
  return `${Number(r).toFixed(2)}×`;
});

function goKline(m) {
  uni.navigateTo({ url: `/pages/sector/kline?code=${m.code}&name=${encodeURIComponent(m.name)}` });
}
</script>
<template>
  <view class="p">
    <view class="segs">
      <text v-for="w in WINDOWS" :key="w" class="seg" :class="{ on: w === win }" @click="pickWindow(w)">近{{ w }}日</text>
    </view>

    <text v-if="err" class="err">{{ err }}</text>

    <view v-if="board" class="card">
      <view class="r1">
        <text class="big">{{ signedPct(board.ret_pct) }}</text>
        <text class="sub2">{{ board.kind === 'industry' ? '行业' : '概念' }} · {{ board.stock_count }}/{{ board.member_count }}只有效</text>
      </view>
      <view class="kv3">
        <view class="k"><text class="n up">{{ board.up_count }}</text><text class="l">上涨</text></view>
        <view class="k"><text class="n down">{{ board.down_count + board.flat_count }}</text><text class="l">下跌/平</text></view>
        <view class="k"><text class="n">{{ board.limit_up_count }}</text><text class="l">区间涨停</text></view>
      </view>
      <view class="kv3">
        <view class="k"><text class="n sm">{{ yiText(board.turnover) }}</text><text class="l">区间成交额</text></view>
        <view class="k"><text class="n sm">{{ ratioText }}</text><text class="l">较前一期</text></view>
        <view class="k"><text class="n sm" :class="pctCls(board.median_ret_pct)">{{ signedPct(board.median_ret_pct) }}</text><text class="l">中位数涨幅</text></view>
      </view>
      <text class="edge">领涨 {{ board.leader_name }} {{ signedPct(board.leader_ret) }} ｜ 领跌 {{ board.lagger_name }} {{ signedPct(board.lagger_ret) }}</text>
    </view>

    <view v-if="!board && !err" class="empty">{{ loading ? '加载中…' : '该窗口下没有板块数据' }}</view>

    <view v-for="m in rows" :key="m.code" class="trow" @click="goKline(m)">
      <view class="lft">
        <text class="nm">{{ m.name }}</text>
        <text class="cd">{{ m.code }}<text v-if="m.limitUp" class="tag">涨停</text><text v-if="m.days < win" class="tag gray">{{ m.days }}日交易</text></text>
      </view>
      <view class="rgt">
        <text class="rt2" :class="pctCls(m.ret)">{{ m.retText }}</text>
        <text class="px">{{ m.close === null ? '—' : m.close.toFixed(2) }} · {{ yiText(m.turnover) }}</text>
      </view>
    </view>

    <view v-if="rows.length" class="foot">点个股看日 K · 数据日 {{ board && board.asof ? board.asof : '' }}</view>
  </view>
</template>
<style>
.p { min-height: 100vh; background: #f4f6fb; padding: 20rpx 24rpx 60rpx; box-sizing: border-box; }
.segs { display: flex; gap: 16rpx; margin-bottom: 20rpx; }
.seg { flex: 1; text-align: center; font-size: 26rpx; color: #5b6270; background: #fff; border-radius: 14rpx; padding: 14rpx 0; }
.seg.on { color: #fff; background: #2b5cff; font-weight: 600; }
.err { display: block; font-size: 26rpx; color: #e64545; margin-bottom: 16rpx; }
.empty { background: #fff; border-radius: 20rpx; padding: 60rpx 0; text-align: center; color: #a2a9b5; font-size: 26rpx; }
.card { background: #fff; border-radius: 20rpx; padding: 26rpx 24rpx; margin-bottom: 20rpx; }
.r1 { display: flex; align-items: baseline; gap: 16rpx; }
.r1 .big { font-size: 46rpx; font-weight: 700; color: #1f2329; }
.r1 .sub2 { font-size: 24rpx; color: #8a919f; }
.kv3 .n.sm { font-size: 28rpx; }
.edge { display: block; margin-top: 16rpx; font-size: 22rpx; color: #8a919f; }
.trow {
  background: #fff; border-radius: 18rpx; padding: 22rpx 24rpx; margin-bottom: 14rpx;
  display: flex; align-items: center; justify-content: space-between;
}
.trow:active { background: #f7f9ff; }
.lft .nm { display: block; font-size: 30rpx; color: #1f2329; font-weight: 600; }
.lft .cd { display: block; font-size: 22rpx; color: #8a919f; margin-top: 6rpx; }
.tag { font-size: 20rpx; color: #e64545; background: #fdecec; border-radius: 8rpx; padding: 2rpx 10rpx; margin-left: 12rpx; }
.tag.gray { color: #8a919f; background: #f0f2f7; }
.rgt { text-align: right; }
.rgt .rt2 { display: block; font-size: 32rpx; font-weight: 700; color: #1f2329; }
.rgt .px { display: block; font-size: 22rpx; color: #8a919f; margin-top: 6rpx; }
.foot { text-align: center; color: #a2a9b5; font-size: 22rpx; margin-top: 24rpx; }
.up { color: #e64545; }
.down { color: #1fa35f; }
</style>
