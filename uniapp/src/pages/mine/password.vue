<!-- uniapp/src/pages/mine/password.vue —— 自助改密（对齐 /api/v1/auth/change-password） -->
<script setup>
import { ref } from "vue";
import { useClient } from "../../composables/useClient.js";
import { clearSession } from "../../store/session.js";
const oldPwd = ref(""); const newPwd = ref(""); const msg = ref("");
const { client } = useClient();

async function submit() {
  msg.value = "";
  try {
    await client.post("/api/v1/auth/change-password", {
      body: { old_password: oldPwd.value, new_password: newPwd.value },
    });
    msg.value = "修改成功，请重新登录";
    setTimeout(() => { clearSession(); uni.reLaunch({ url: "/pages/login/index" }); }, 600);
  } catch (e) { msg.value = e.message; }
}
</script>
<template>
  <view class="p">
    <input v-model="oldPwd" type="password" placeholder="原密码" />
    <input v-model="newPwd" type="password" placeholder="新密码" />
    <button :disabled="!oldPwd || !newPwd" @click="submit">确认修改</button>
    <text v-if="msg">{{ msg }}</text>
  </view>
</template>
