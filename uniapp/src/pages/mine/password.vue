<!-- uniapp/src/pages/mine/password.vue —— 自助改密（对齐 /api/v1/auth/change-password） -->
<script setup>
import { ref } from "vue";
import { useClient } from "../../composables/useClient.js";
import { clearSession } from "../../store/session.js";
const oldPwd = ref(""); const newPwd = ref(""); const msg = ref(""); const okMsg = ref("");
const busy = ref(false);
const { client } = useClient();

async function submit() {
  msg.value = ""; okMsg.value = "";
  if (newPwd.value === oldPwd.value) { msg.value = "新密码不能与原密码相同"; return; }
  if (busy.value) return;
  busy.value = true;
  try {
    await client.post("/api/v1/auth/change-password", {
      body: { old_password: oldPwd.value, new_password: newPwd.value },
    });
    okMsg.value = "修改成功，即将重新登录";
    setTimeout(() => { clearSession(); uni.reLaunch({ url: "/pages/login/index" }); }, 900);
  } catch (e) { msg.value = e.message; } finally { busy.value = false; }
}
</script>
<template>
  <view class="p">
    <view class="form">
      <view class="field">
        <text class="label">原密码</text>
        <input v-model="oldPwd" type="password" placeholder="请输入原密码" />
      </view>
      <view class="field">
        <text class="label">新密码</text>
        <input v-model="newPwd" type="password" placeholder="请输入新密码" />
      </view>
      <text v-if="msg" class="err">{{ msg }}</text>
      <text v-if="okMsg" class="ok">{{ okMsg }}</text>
      <button class="submit" :disabled="!oldPwd || !newPwd || busy" @click="submit">
        {{ busy ? "提交中…" : "确认修改" }}
      </button>
    </view>
  </view>
</template>
<style>
.field { margin-bottom: 30rpx; }
.field .label { display: block; font-size: 26rpx; color: #5c6470; font-weight: 600; margin-bottom: 12rpx; }
.field input { background: #f4f6fb; border-radius: 16rpx; padding: 22rpx 26rpx; font-size: 30rpx; }
.submit {
  width: 100%; margin-top: 10rpx; line-height: 3;
  background: linear-gradient(90deg, #2b5cff, #5b8cff);
  color: #fff; border-radius: 16rpx; font-size: 30rpx; letter-spacing: 4rpx;
}
.submit[disabled] { opacity: 0.55; }
.ok {
  display: block; color: #1fa35f; background: #e8f7ee;
  border-radius: 12rpx; padding: 14rpx 20rpx; margin: 18rpx 0; font-size: 26rpx;
}
</style>
