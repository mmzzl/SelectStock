<!-- uniapp/src/pages/login/index.vue -->
<script setup>
import { ref } from "vue";
import { useClient } from "../../composables/useClient.js";

const username = ref(""); const password = ref(""); const code = ref("");
const captchaId = ref(""); const captchaImg = ref(""); const err = ref("");
const { client, setSession } = useClient();

async function refreshCaptcha() {
  code.value = "";
  try {
    const data = await client.get("/api/v1/auth/captcha");
    captchaId.value = data.captcha_id;
    captchaImg.value = data.image_base64 ? `data:image/png;base64,${data.image_base64}` : "";
  } catch { /* 验证码接口不可用时仍可尝试登录（后端 captcha 关闭场景） */ }
}
refreshCaptcha();

async function submit() {
  err.value = "";
  try {
    const data = await client.post("/api/v1/auth/login", {
      body: { username: username.value, password: password.value,
              captcha_id: captchaId.value || undefined, captcha_code: code.value || undefined },
    });
    setSession(data);
    try { data.user = await client.get("/api/v1/auth/me"); setSession(data); } catch { /* me 失败不阻断登录 */ }
    uni.switchTab({ url: "/pages/holdings/index" });
  } catch (e) {
    err.value = e.message;
    refreshCaptcha();   // 验证码用后即焚，失败必刷新
  }
}
</script>
<template>
  <view class="p">
    <input v-model="username" placeholder="用户名" />
    <input v-model="password" type="password" placeholder="密码" />
    <view class="cap" v-if="captchaImg">
      <input v-model="code" placeholder="验证码" />
      <image :src="captchaImg" mode="aspectFit" class="capimg" @click="refreshCaptcha" />
    </view>
    <button :disabled="!username || !password" @click="submit">登录</button>
    <text v-if="err" class="err">{{ err }}</text>
  </view>
</template>
<style>
.cap { display: flex; align-items: center; gap: 12rpx; }
.capimg { width: 180rpx; height: 64rpx; }
</style>
