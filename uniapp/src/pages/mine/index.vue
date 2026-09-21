<!-- uniapp/src/pages/mine/index.vue —— 退出 + 修改密码 + 检查更新（H5/App 双分支，U11） -->
<script setup>
import { ref } from "vue";
import { loadSession, clearSession } from "../../store/session.js";
import { useClient } from "../../composables/useClient.js";
import { checkUpdate } from "../../logic/version.js";

const me = ref(loadSession().me || {});
function logout() { clearSession(); uni.reLaunch({ url: "/pages/login/index" }); }
const goPassword = () => uni.navigateTo({ url: "/pages/mine/password" });

// #ifdef APP-PLUS
function isAndroidApp() {
  try { return uni.getSystemInfoSync().osName === "android"; } catch { return false; }
}
function downloadAndInstall(url, md5, pkgType) {
  const dtask = plus.downloader.createDownload(url, { filename: "_doc/update/" + url.split("/").pop() },
    (d, status) => {
      if (status === 200) {
        const path = d.filename;
        if (pkgType === "wgt") plus.runtime.install(path, { force: true }, () => uni.showToast({ title: "热更完成，重启生效" }));
        else plus.runtime.install(path, { force: true });   // apk/ipa 整包
      } else uni.showToast({ title: "下载失败", icon: "none" });
    });
  dtask.start();
}
function appUpdate() {
  const local = Number(globalThis.__APP_VER_CODE__ || 100);
  useClient().upgrade.latest({ platform: (isAndroidApp() ? "android" : "ios"), version_code: local })
    .then((info) => {
      const u = checkUpdate({ local, remote: info?.latest });
      if (!u.hasUpdate) { uni.showToast({ title: "已是最新版本" }); return; }
      const url = u.latest?.url || ""; const md5 = u.latest?.md5 || "";
      if (!url) { uni.showToast({ title: "暂无可下载包", icon: "none" }); return; }
      uni.showModal({ title: "发现新版本" + (u.latest?.version_name || ""),
        content: u.latest?.release_notes || "", showCancel: !u.forced,   // forced 不可关闭（强制升级）
        success: (o) => { if (o.confirm || u.forced) downloadAndInstall(url, md5, u.packageType); } });
    })
    .catch(() => uni.showToast({ title: "检查更新失败", icon: "none" }));
}
const checkUpgrade = appUpdate;
// #endif

// #ifndef APP-PLUS
function checkUpgrade() {
  const local = Number(globalThis.__H5_VER_CODE__ || 100);
  useClient().upgrade.latest({ platform: "h5", version_code: local })
    .then((info) => {
      const u = checkUpdate({ local, remote: info?.latest });
      uni.showToast({ title: u.hasUpdate ? "发现新版本，请刷新页面" : "已是最新版本", icon: "none" });
    })
    .catch(() => uni.showToast({ title: "检查更新失败", icon: "none" }));
}
// #endif
</script>
<template>
  <view class="p">
    <text>用户：{{ me.username || me.id || "-" }}</text>
    <button @click="goPassword">修改密码</button>
    <button @click="checkUpgrade">检查更新</button>
    <button @click="logout">退出登录</button>
  </view>
</template>
