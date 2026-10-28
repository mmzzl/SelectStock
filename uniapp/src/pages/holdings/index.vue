<!-- uniapp/src/pages/holdings/index.vue —— 持仓总览：按诊断建议分组；数值一律后端/插件下发（R1-3） -->
<script setup>
import { ref, computed } from "vue";
import { onShow } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";
import { convictionHead, convictionLevelClass, convictionLevelText, convictionNote,
         buyLevelText, dayRankText, gameText, hasConviction, levelNoteText, priceStampText,
         sellTriggerText, takeProfitText, toHoldingRow, toPnlView } from "../../logic/view.js";

const rows = ref([]); const loading = ref(false); const err = ref("");
const summary = ref(null);           // 来自 /holdings/pnl 的 summary，前端不做加总
const rankMeta = ref(null);          // /selection/rank 的整表元信息（date/pool/universe/卖出线）
const rankErr = ref("");             // 名次查询失败的原因，和持仓列表的失败分开说
const { holdings, selection } = useClient();
const today = () => new Date().toISOString().slice(0, 10);

onShow(async () => {
  loading.value = true; err.value = "";
  try {
    const data = await holdings.list({});
    rows.value = (data.items || data || []).map(toHoldingRow);
    // 汇总条与列表分开取：插件不可用时只降级掉汇总，不影响持仓列表
    try { summary.value = toPnlView(await holdings.pnl({ natural_day: today() })); } catch { summary.value = null; }
  } catch (e) { err.value = e.message; } finally { loading.value = false; }
  // 名次不挂在 loading 上：这条接口现在**不会等打分**（引擎当日榜单没算好时立刻回 ready=false
  // 并自己起后台预热，全市场约 2,900 只冷打分要几百秒，放进请求里一定被超时打断）。
  // 先让持仓列表出来，名次到了再填，取不到就只说名次这件事的状态。
  loadRanks();
});

async function loadRanks() {
  const codes = rows.value.map((r) => r.code).filter(Boolean);
  if (!codes.length) { rankMeta.value = null; rankErr.value = ""; return; }
  if (codes.length > 60) {
    // 接口一次最多 60 只（超过会报错）。这里不悄悄丢掉多出来的持仓：
    // 少查几只票的名次，看起来像「那几只没上榜」，而实际是根本没去查。
    rankMeta.value = null; rankErr.value = `榜内名次一次最多查 60 只，当前持仓 ${codes.length} 只`;
    return;
  }
  rankErr.value = "";
  try {
    rankMeta.value = await selection.rank({ codes: codes.join(",") });
  } catch (e) { rankMeta.value = null; rankErr.value = e.message || "网络请求失败"; }
}

const ranksByCode = computed(() => new Map((rankMeta.value?.items || []).map((it) => [it.code, it])));
// 引擎根本不提供名次时（自研内核），这句话在页头说一次就够，不必每行重复
const rankSupported = computed(() => Boolean(rankMeta.value?.supported));
const rankHead = computed(() => {
  const m = rankMeta.value;
  if (!m) return rankErr.value ? `榜内名次没取到：${rankErr.value}` : "";
  if (!m.supported) return `榜内名次 · ${m.message || "当前选股引擎不提供榜内名次"}`;
  // ready=false：引擎当日还没算完全票池打分（放开全市场后第一次要几百秒），它正在后台算。
  // 这一句必须和「都没上榜」分开显示——后者会让人以为持仓集体跌出卖出线该卖了。
  if (m.ready === false) return `榜内名次 · ${m.message || "当日榜单还在计算中"} · 稍后重进本页`;
  return `榜内名次是 ${m.date} 收盘后引擎对 ${m.pool_size} 只候选池的排序`
    + `（当日打得分出 ${m.universe} 只）· 卖出线取回测同一条：前 ${m.exit_rank} 名`;
});
const rankLine = (code) => (rankSupported.value ? dayRankText(ranksByCode.value.get(code), rankMeta.value) : "");

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
// 现价后面带上「16:14 实时」/「10-11 收盘」：光看数字分不出它是新价还是昨天的收盘
const stamp = (r) => { const t = priceStampText(r); return t ? `（${t}）` : ""; };
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
          <!-- 名次的口径必须写在旁边：没有这句，「第 7 名」会被读成选股名单里的那个 rank -->
          <view v-if="rankHead" class="bs">{{ rankHead }}</view>
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
            <text class="d">现价 {{ r.latestPrice ?? "—" }}{{ stamp(r) }} · 成本 {{ r.cost ?? "—" }} · {{ r.shares ?? "—" }} 股</text>
            <text class="n">{{ r.name }}（{{ r.code }}）</text>
            <text v-if="r.actionReason" class="sub">{{ r.actionReason }}</text>
            <!-- 当日榜内名次（引擎给整个票池排序）：名次取不到时这一行给的是原因，不是猜的名次 -->
            <text v-if="rankLine(r.code)" class="sub">{{ rankLine(r.code) }}</text>
            <text v-if="r.agg && r.agg.realizedPnl" class="sub acc">已实现 {{ signed(r.agg.realizedPnl) }} · 含已实现合计 {{ signed(r.agg.totalPnl) }}</text>
            <view v-if="hasConviction(r)" class="conv">
              <view class="conv-top">
                <text class="conv-head" :class="{ strong: r.convictionPct !== null }">{{ convictionHead(r) }}</text>
                <text v-if="convictionLevelText(r)" class="conv-lv" :class="convictionLevelClass(r)">{{ convictionLevelText(r) }}</text>
              </view>
              <text v-if="convictionNote(r)" class="conv-note">{{ convictionNote(r) }}</text>
              <text v-if="buyLevelText(r)" class="conv-line chip">筹码价位 · {{ buyLevelText(r) }}</text>
              <text v-if="levelNoteText(r)" class="conv-note">{{ levelNoteText(r) }}</text>
              <text v-if="sellTriggerText(r)" class="conv-line trig">止损 · {{ sellTriggerText(r) }}</text>
              <text v-if="takeProfitText(r)" class="conv-line tp">止盈 · {{ takeProfitText(r) }}</text>
              <text v-if="gameText(r)" class="conv-line game">{{ gameText(r) }}</text>
            </view>
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
.trow .acc { color: #2b5cff; }
/* 止损/止盈/可信度块样式见 styles/common.css 的 .conv（两页共用） */
</style>
