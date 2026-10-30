<!-- uniapp/src/pages/selection/index.vue -->
<script setup>
import { computed, ref } from "vue";
import { onShow, onHide, onUnload } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";
import { toSelectionResultRow, toStrategyOption, riskText, riskClass, isNa, sortByRiskDesc, buyLevelText, levelNoteText, orderPlanText, riskAdjustText, posteriorText, gateText, scoreText } from "../../logic/view.js";

const { selection } = useClient();
const strategies = ref([]);        // [{ name, label, desc }]
const chosen = ref([]);
const job = ref(null);          // { jobId, status, results, ... }
const results = ref([]);
const polling = ref(false);
const err = ref("");
let timer = null;

onShow(async () => {
  // 任务还在跑就切走过（onHide 停了轮询），回来先续上，不然页面永远停在「计算中」不动。
  if (job.value && !["done", "failed"].includes(job.value.status)) startPoll(job.value.jobId);
  // 每次进页都刷新：策略下线（例如 hot_pref）后，旧选项不能继续留在页面上被点中。
  // 刷新失败时保留旧列表并报错，不能把「插件连不上」显示成「没有策略」。
  try {
    const details = await selection.strategyDetail() || [];
    if (!details.length) throw new Error("选股插件没有可用策略");
    strategies.value = details.map(toStrategyOption);
  } catch (e) {
    try {
      const names = await selection.strategies() || [];
      if (!names.length) throw new Error("选股插件没有可用策略");
      strategies.value = names.map((n) => ({ name: n, label: n, desc: "" }));
    } catch (e2) { err.value = e2.message || "选股插件不可用"; return; }
  }
  err.value = "";
  chosen.value = chosen.value.filter((n) => strategies.value.some((s) => s.name === n));
});
onHide(() => {
  // 这是 tabBar 页，切走只触发 onHide、不触发 onUnload。任务 running 期间那台 2 秒轮询
  // 不清掉就会在后台一直抢这条公网隧道（单请求实测 0.23~0.92 秒），回来再续上。
  clearInterval(timer); timer = null;
});
onUnload(() => { clearInterval(timer); timer = null; });

async function trigger() {
  err.value = "";
  try {
    const out = await selection.run(chosen.value.length ? chosen.value : null);
    job.value = { jobId: out.job_id, status: out.status, results: [] };
    startPoll(out.job_id);
  } catch (e) { err.value = e.message; }
}
function startPoll(jobId) {
  if (timer) clearInterval(timer);        // 连点两次不能留两条轮询同时写同一个 job
  polling.value = true;
  timer = setInterval(async () => {
    try {
      const d = await selection.runDetail(jobId);
      job.value = { jobId: d.job_id, status: d.status, strategies: d.strategies,
                    error: d.error || "", results: d.results || [] };
      results.value = (d.results || []).map(toSelectionResultRow);
      if (d.status === "done" || d.status === "failed") { clearInterval(timer); timer = null; polling.value = false; }
    } catch (e) { clearInterval(timer); timer = null; polling.value = false; err.value = e.message; }
  }, 2000);
}
const goHistory = () => uni.navigateTo({ url: "/pages/selection/history" });

// 展示顺序按预期 10 日超额降序（后端 rank 是策略规则名次，头部无信息，见 view.js 的说明）
const ordered = computed(() => sortByRiskDesc(results.value));

// 结果里的 strategy 是插件策略名，中文标签统一取自 /strategy-detail 的 description。
// 旧任务里的 hot_pref 已下线，历史页/旧结果里要标明，不能再让人以为它还在出票。
const labelOf = (s) => strategies.value.find((x) => x.name === s)?.label
  || (s === "hot_pref" ? "热门偏好榜（已下线）" : s || "信号");
const fixed2 = (v) => (v === null || v === undefined || Number.isNaN(Number(v)) ? "—" : Number(v).toFixed(2));
</script>
<template>
  <view>
    <view class="band">
      <view class="band-in">
        <view>
          <view class="bt">智能选股</view>
          <view class="bs">选择策略后触发 · 结果由选股插件产出</view>
        </view>
        <button class="band-btn" @click="goHistory">历史回看</button>
      </view>
    </view>

    <view class="p">
      <view class="card">
        <view class="sec" style="margin-top:0">选择策略（不选则全部）</view>
        <view>
          <view v-for="s in strategies" :key="s.name" class="chip" :class="{ on: chosen.includes(s.name) }"
                @click="chosen = chosen.includes(s.name) ? chosen.filter(x => x !== s.name) : [...chosen, s.name]">{{ s.label }}</view>
          <text v-if="!strategies.length" class="reason">策略列表加载中或插件不可用</text>
        </view>
        <button class="btn-big" :disabled="polling" @click="trigger">
          {{ polling ? "选股中，请稍候…" : "开 始 选 股" }}
        </button>
        <view v-if="job" class="row1" style="margin-top:8rpx">
          <text class="st" :class="job.status"><text class="dot" />任务 {{ job.status }}</text>
          <text class="time">{{ job.jobId }}</text>
        </view>
      </view>

      <text v-if="err" class="err">{{ err }}</text>
      <text v-if="job && job.error" class="err">{{ job.error }}</text>

      <view v-if="results.length" class="sec">选股结果（{{ results.length }} 只）</view>
      <view v-if="results.length" class="sortnote">
        框内数字是这只票在它那条榜里的名次。模型榜排在最前，按护栏后的名次升序；
        旧任务里可能还留着已下线的「热门偏好榜」行，只作历史回看，不再进盘中池。
        它当年按你点名的四条重排（热门板块 + 新闻利好 + 排除小市值 + 趋势向上），
        和模型榜用的是两把不同的尺子，不是重复计票。
        「预期 10 日超额」是买入风险模型的预测值：经验证的是「低于阈值的剔除」，高分不等于必涨；
        显示「—」的表示模型没能判分（多为场内基金），排在最后。
        显示「不适用」的表示当前选股引擎根本不算这项（qlib 只出横截面排序分），
        它和「—」（算了但没判出来）是两回事，更不能读成「预测为零/没有风险」。
        「排序分」是满分 100 的刻度，只用来决定谁排在谁前面，不是把握的大小：
        引擎落库的是这只票在当日整个候选池里的位置（0~1），第一名正好 100 分、第二名 99.7 分，
        差的只是名次；自研引擎给的是规则强度，几条同时满足会顶到满分不再往上走。
        两种都不是「上涨概率」，100 分也不代表必涨。
        「热门偏好榜」的分数是四路加权（板块热度 45%、趋势 35%、新闻利好 20%），
        市值、趋势、板块是三道硬闸，闸不过的票连分数都不给；它用的板块热度与新闻利好
        都是**前一天**落地的（板块热度 18:30、新闻 22:00，都晚于 17:30 的选股），
        而板块热度没有历史存档、流通市值只有 2026-10-12 起的日快照，
        所以这条榜**没有回测过**——它是按你的偏好排的名次，不是经验证有超额的名次。
        「买入参考/跌破失效」是策略判据里的日均线位（收盘口径），只用来对照盘面，不是实时挂单价；
        qlib 只出排序分，没有这类判据价位，所以那行写「不适用」——它不等于「这笔买入不会错」，
        这条名单上能对照的价位只有上方「基准价」（信号日收盘，回测也是按这个价成交的）。
        价位止损别自己补：同一只模型的留出段（300 只池、673 个信号日、每日前 30 名、2.0 万笔、毛收益未扣成本）
        实测名单里的票 10 日内最深下探中位只有 −3.7%，设 −5%/−10%/−20% 的止损线每笔分别少赚
        0.34/0.21/0.06 个百分点（按 10 日重叠折算后 t = −3.1/−3.2/−2.1），被止损的票里一半以上拿满 10 日更赚；
        且那里按「盘中触线就成交」记，跳空低开时真实成交更差，所以那三个数还是偏乐观的。
        这条名单验证过的换出纪律是名次（跌出第 100 名换出），不是价位。
        「建议股数/预估成交/预估滑点」是风控层给的下单口径：涨停买不进、盘子太薄、超过单票上限的信号
        已经在风控这一步被剔除，不会出现在列表里；橙色一句表示风控改过建议下单量。
        灰色一句「依据」说的是这条判据本身历史上值多少钱（T+10 相对当日全样本的超额，
        按时代与股票抽样各跑一遍合成），不是这只票的评分；写「未标定」或「会翻号」时
        表示这条判据现在没有可复现的历史优势，名次只是规则排序，不能当买入理由。
      </view>
      <view v-for="r in ordered" :key="`${r.strategy}|${r.code}`" class="card">
        <view class="row1">
          <text><text v-if="r.rank" class="rank">{{ r.rank }}</text> {{ r.name }}（{{ r.code }}）</text>
          <text class="badge sig">{{ labelOf(r.strategy) }}</text>
        </view>
        <view class="kv3">
          <view class="k"><text class="n">{{ fixed2(r.price) }}</text><text class="l">基准价</text></view>
          <view class="k"><text class="n">{{ scoreText(r) }}</text><text class="l">排序分/满分100</text></view>
          <view class="k"><text class="n date" :class="riskClass(r.risk, isNa(r, 'risk_ex10_pp'))">{{ riskText(r.risk, isNa(r, 'risk_ex10_pp')) }}</text><text class="l">预期10日超额</text></view>
          <view class="k"><text class="n date">{{ r.asof || "—" }}</text><text class="l">基准交易日</text></view>
        </view>
        <text v-if="buyLevelText(r)" class="levels">{{ buyLevelText(r) }}</text>
        <text v-if="levelNoteText(r)" class="note">{{ levelNoteText(r) }}</text>
        <text v-if="orderPlanText(r)" class="levels">{{ orderPlanText(r) }}</text>
        <text v-if="riskAdjustText(r)" class="adjust">{{ riskAdjustText(r) }}</text>
        <text class="posterior">{{ posteriorText(r) }}</text>
        <text v-if="gateText(r)" class="gate">{{ gateText(r) }}</text>
        <text class="reason">{{ r.reason }}</text>
      </view>
      <view v-if="job && !results.length && polling" class="empty">全市场扫描中，请稍候…</view>
    </view>
  </view>
</template>
<style>
.rank {
  display: inline-block; width: 40rpx; height: 40rpx; line-height: 40rpx;
  text-align: center; border-radius: 10rpx;
  background: #eef3ff; color: #2b5cff; font-size: 24rpx; font-weight: 700;
}
.kv3 .n.date { font-size: 24rpx; font-weight: 600; color: #5c6470; }
.levels { display: block; margin-top: 8rpx; font-size: 24rpx; color: #2b5cff; }
.note { display: block; margin-top: 4rpx; font-size: 22rpx; color: #8a8f99; }
.adjust { display: block; margin-top: 6rpx; font-size: 22rpx; color: #c8641e; }
.posterior { display: block; margin-top: 6rpx; font-size: 22rpx; color: #8a919f; }
.gate { display: block; margin-top: 4rpx; font-size: 22rpx; color: #b8821f; }
.sortnote {
  font-size: 22rpx; color: #8a919f; line-height: 1.6;
  margin: -6rpx 4rpx 16rpx;
}
</style>
