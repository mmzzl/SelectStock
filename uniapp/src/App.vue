<!-- uniapp/src/App.vue —— 守卫入口（逻辑见 logic/guard.js；版本钩子在 U11 追加） -->
<script>
import { isLoggedIn } from "./store/session.js";   // 依赖 U2，先建文件占位再接线
export default {
  onLaunch() {
    const pages = getCurrentPages?.() || [];
    const cur = pages.length ? pages[pages.length - 1].route : "";
    if (!isLoggedIn() && cur !== "pages/login/index") {
      uni.reLaunch({ url: "/pages/login/index" });
    }
  },
};
</script>
<template><view /></template>