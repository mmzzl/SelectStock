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
const WS_URL = globalThis.__VITE_WS_URL__ || "ws://127.0.0.1:8000/ws/notify";
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
  <view class="p">
    <view class="bar">
      <text>未读 {{ unread }}</text>
      <button size="mini" @click="readAll">全部已读</button>
    </view>
    <text v-if="err" class="err">{{ err }}</text>
    <view v-for="m in rows" :key="m.id" class="card" @click="read(m)">
      <text :class="{ unread: m.unread }">{{ m.title }}</text>
      <text class="reason">{{ m.content }}</text>
    </view>
    <view v-if="loaded && !rows.length" class="empty">暂无消息</view>
  </view>
</template>
