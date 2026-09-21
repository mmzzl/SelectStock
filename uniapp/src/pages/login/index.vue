<!-- uniapp/src/pages/login/index.vue -->
<script setup>
import { ref } from "vue";
import { useClient } from "../../composables/useClient.js";

const username = ref(""); const password = ref(""); const err = ref("");
const { client, setSession } = useClient();

async function submit() {
  err.value = "";
  try {
    const data = await client.post("/api/v1/auth/login", { body: { username: username.value, password: password.value } });
    setSession(data);
    try { data.user = await client.get("/api/v1/auth/me"); setSession(data); } catch { /* me 失败不阻断登录 */ }
    uni.switchTab({ url: "/pages/holdings/index" });
  } catch (e) { err.value = e.message; }
}
</script>
<template>
  <view class="p">
    <input v-model="username" placeholder="用户名" />
    <input v-model="password" type="password" placeholder="密码" />
    <button :disabled="!username || !password" @click="submit">登录</button>
    <text v-if="err" class="err">{{ err }}</text>
  </view>
</template>
