<!-- uniapp/src/App.vue —— 守卫入口（逻辑见 logic/guard.js）+ H5 启动版本检查（U11） -->
<script>
import { isLoggedIn } from "./store/session.js";
import { useClient } from "./composables/useClient.js";
import { checkUpdate } from "./logic/version.js";

function h5VersionCheck() {
  const local = Number(import.meta.env.VITE_H5_VER_CODE || 100);   // 构建注入，缺省随 manifest
  useClient().upgrade.latest({ platform: "h5", version_code: local })
    .then((info) => {
      const u = checkUpdate({ local, remote: info?.latest });   // 后端返回 {has_update, latest}
      if (u.hasUpdate) {
        uni.showModal({ title: "版本更新", content: u.latest?.release_notes || "发现新版本",
          confirmText: "刷新", success: (o) => { if (o.confirm) uni.reLaunch({ url: "/pages/holdings/index" }); } });
      }
    }).catch(() => {});
}

export default {
  onLaunch() {
    const pages = getCurrentPages?.() || [];
    const cur = pages.length ? pages[pages.length - 1].route : "";
    if (!isLoggedIn() && cur !== "pages/login/index") {
      uni.reLaunch({ url: "/pages/login/index" });
    }
    h5VersionCheck();
  },
};
</script>
<template><view /></template>
<style>
@import "./styles/common.css";
</style>
