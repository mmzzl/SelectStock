<!-- uniapp/src/pages/mine/index.vue —— 退出 + 更新入口占位（U11 增强版本检查） -->
<script setup>
import { ref } from "vue";
import { loadSession, clearSession } from "../../store/session.js";
const me = ref(loadSession().me || {});
function logout() { clearSession(); uni.reLaunch({ url: "/pages/login/index" }); }
</script>
<template>
  <view class="p">
    <text>用户：{{ me.username || me.id || "-" }}</text>
    <button @click="logout">退出登录</button>
  </view>
</template>
