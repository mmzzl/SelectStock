<!-- uniapp/src/pages/login/index.vue -->
<script setup>
import { ref } from "vue";
import { useClient } from "../../composables/useClient.js";

const username = ref(""); const password = ref(""); const code = ref("");
const captchaId = ref(""); const captchaImg = ref(""); const err = ref("");
const busy = ref(false);
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
  if (busy.value) return;
  err.value = ""; busy.value = true;
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
  } finally { busy.value = false; }
}
</script>
<template>
  <view class="login">
    <view class="hero">
      <view class="logo">选</view>
      <view class="title">智能选股</view>
      <view class="subtitle">持仓诊断 · 量化选股 · 利润分析</view>
    </view>
    <view class="panel">
      <view class="field">
        <text class="label">用户名</text>
        <input v-model="username" placeholder="请输入用户名" placeholder-class="ph" />
      </view>
      <view class="field">
        <text class="label">密码</text>
        <input v-model="password" type="password" placeholder="请输入密码" placeholder-class="ph" />
      </view>
      <view class="field" v-if="captchaImg">
        <text class="label">验证码</text>
        <view class="cap">
          <input v-model="code" placeholder="点击图形刷新" placeholder-class="ph" />
          <image :src="captchaImg" mode="aspectFit" class="capimg" @click="refreshCaptcha" />
        </view>
      </view>
      <text v-if="err" class="err">{{ err }}</text>
      <button class="submit" :disabled="!username || !password || busy" @click="submit">
        {{ busy ? "登录中…" : "登 录" }}
      </button>
    </view>
    <view class="foot">登录后自动进入持仓总览</view>
  </view>
</template>
<style>
.login {
  min-height: 100vh;
  background: linear-gradient(180deg, #2b5cff 0%, #5b8cff 38%, #f4f6fb 38%, #f4f6fb 100%);
  padding: 0 48rpx;
  box-sizing: border-box;
}
.hero { padding: 96rpx 8rpx 40rpx; color: #fff; }
.logo {
  width: 96rpx; height: 96rpx; border-radius: 26rpx;
  background: rgba(255, 255, 255, 0.22);
  color: #fff; font-size: 48rpx; font-weight: 700;
  display: flex; align-items: center; justify-content: center;
  margin-bottom: 24rpx;
}
.title { font-size: 48rpx; font-weight: 700; letter-spacing: 2rpx; }
.subtitle { font-size: 26rpx; opacity: 0.85; margin-top: 10rpx; }
.panel {
  background: #fff; border-radius: 28rpx; padding: 44rpx 36rpx;
  box-shadow: 0 16rpx 48rpx rgba(43, 92, 255, 0.14);
}
.field { margin-bottom: 36rpx; }
.label { display: block; font-size: 26rpx; color: #5c6470; margin-bottom: 14rpx; font-weight: 600; }
.field input {
  background: #f4f6fb; border-radius: 16rpx; padding: 22rpx 26rpx;
  font-size: 30rpx; border: 2rpx solid transparent;
}
.field input:focus { border-color: #2b5cff; background: #fff; }
.ph { color: #a2a9b5; }
.cap { display: flex; align-items: center; gap: 20rpx; }
.cap input { flex: 1; }
.capimg {
  width: 220rpx; height: 88rpx; border-radius: 16rpx;
  background: #f4f6fb; border: 2rpx solid #e2e6ef;
}
.submit {
  width: 100%; margin-top: 16rpx; line-height: 3.1;
  background: linear-gradient(90deg, #2b5cff, #5b8cff);
  color: #fff; border-radius: 18rpx; font-size: 32rpx; letter-spacing: 8rpx;
  box-shadow: 0 10rpx 26rpx rgba(43, 92, 255, 0.32);
}
.submit[disabled] { opacity: 0.55; box-shadow: none; }
.foot { text-align: center; color: #a2a9b5; font-size: 24rpx; margin-top: 44rpx; }
</style>
