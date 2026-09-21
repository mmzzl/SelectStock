<!-- uniapp/src/pages/messages/index.vue -->
<script setup>
import { ref } from "vue";
import { onShow, onUnload } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";
import { createNotifySocket } from "../../logic/ws.js";
import { toMessageRow } from "../../logic/view.js";
import { loadSession } from "../../store/session.js";

const rows = ref([]); const unread = ref(0); const loaded = ref(false);
const err = ref("");
const { messages } = useClient();
const WS_URL = globalThis.__VITE_WS_URL__ || "ws://127.0.0.1:18080/ws/notify";
let sock = null;

async function reload(sinceId) {
  const q = { page: 1, size: 50 };
  if (sinceId) q.since_id = sinceId;
  try {
    const data = await messages.list(q);
    rows.value = (data.items || []).map(toMessageRow);
    unread.value = data.unread_count ?? 0;
    loaded.value = true;
  } catch (e) { err.value = e.message; }
}

// hello/重连：按 latest_inbox_id 增量补齐（since_id 只返回更新的消息，不能整页替换）
async function sync(sinceId) {
  if (!sinceId || !loaded.value) { reload(); return; }
  try {
    const data = await messages.list({ page: 1, size: 50, since_id: sinceId });
    const extra = (data.items || []).map(toMessageRow);
    if (extra.length) rows.value = [...extra, ...rows.value];
    unread.value = data.unread_count ?? unread.value;
  } catch (e) { err.value = e.message; }
}

function ensureWs() {
  if (sock) return;
  sock = createNotifySocket({
    url: WS_URL,
    token: loadSession().token,
    onReconnect: ({ sinceId }) => sync(sinceId || undefined),   // hello 即按 latest_inbox_id 补齐
    onMessage: () => reload(),                                  // 新消息帧 → 刷新
  });
  sock.connect();
}

onShow(() => { reload(); ensureWs(); });
onUnload(() => { sock?.close(); sock = null; });

async function read(m) {
  if (!m.unread) return;
  try { await messages.markRead(m.id); reload(); } catch (e) { err.value = e.message; }
}
async function readAll() {
  try { await messages.markAllRead(); reload(); } catch (e) { err.value = e.message; }
}
</script>
<template>
  <view>
    <view class="band">
      <view class="band-in">
        <view>
          <view class="bt">消息中心</view>
          <view class="bs">
            <text v-if="unread">{{ unread }} 条未读</text>
            <text v-else>全部已读</text>
          </view>
        </view>
        <button class="band-btn" @click="readAll">全部已读</button>
      </view>
    </view>

    <view class="p">
      <text v-if="err" class="err">{{ err }}</text>
      <view v-for="m in rows" :key="m.id" class="card msg" @click="read(m)">
        <view class="row1">
          <view class="msg-t">
            <text v-if="m.unread" class="dot-red" />
            <text class="title" :class="{ unread: m.unread }">{{ m.title }}</text>
          </view>
          <text class="time">{{ m.createdAt || "" }}</text>
        </view>
        <text class="reason">{{ m.content }}</text>
      </view>
      <view v-if="loaded && !rows.length" class="empty">
        <view>暂无消息</view>
        <view class="reason">诊断/选股完成等事件会自动送达</view>
      </view>
    </view>
  </view>
</template>
<style>
.msg .msg-t { display: flex; align-items: center; flex: 1; overflow: hidden; }
.msg .title { font-size: 30rpx; color: #1f2329; }
</style>
