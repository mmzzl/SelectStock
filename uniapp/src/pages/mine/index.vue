<!-- uniapp/src/pages/mine/index.vue —— 资料头部 + 列表菜单 + 检查更新（H5/App 双分支，U11） -->
<script setup>
import { ref } from "vue";
import { onShow } from "@dcloudio/uni-app";
import { loadSession, clearSession } from "../../store/session.js";
import { useClient } from "../../composables/useClient.js";
import { checkUpdate } from "../../logic/version.js";

const me = ref(loadSession().me || {});
const version = ref("");
const { client, setSession } = useClient();

onShow(() => {
  me.value = loadSession().me || {};
  client.get("/api/app/version").then((d) => { version.value = d.version || ""; }).catch(() => {});
  // 刷新用户资料（角色/用户名）
  client.get("/api/v1/auth/me").then((u) => {
    if (u?.id) {
      me.value = u;
      const s = loadSession();
      setSession({ access_token: s.token, refresh_token: s.refresh, user: u });
    }
  }).catch(() => {});
});

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
  const local = Number(import.meta.env.VITE_APP_VER_CODE || 100);
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
  const local = Number(import.meta.env.VITE_H5_VER_CODE || 100);
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
  <view class="mine">
    <view class="hero">
      <view class="avatar">{{ (me.display_name || me.username || "?").slice(0, 1).toUpperCase() }}</view>
      <view class="who">
        <text class="name">{{ me.display_name || me.username || "未登录" }}</text>
        <text class="role">{{ me.is_superuser ? "管理员" : "普通用户" }}</text>
      </view>
    </view>

    <view class="cells">
      <view class="cell" @click="goPassword">
        <text class="t">修改密码</text>
        <text class="arrow">›</text>
      </view>
      <view class="cell" @click="checkUpgrade">
        <text class="t">检查更新</text>
        <text class="v" v-if="version">v{{ version }}</text>
        <text class="arrow">›</text>
      </view>
    </view>

    <view class="cells">
      <view class="cell danger" @click="logout">
        <text class="t">退出登录</text>
        <text class="arrow">›</text>
      </view>
    </view>

    <view class="foot">智能选股 · 数据全部来自服务端</view>
  </view>
</template>
<style>
.mine { min-height: 100vh; background: #f4f6fb; padding-bottom: 60rpx; box-sizing: border-box; }
.hero {
  background: linear-gradient(135deg, #2b5cff, #5b8cff);
  padding: 72rpx 40rpx 96rpx;
  display: flex; align-items: center; gap: 28rpx;
  color: #fff;
}
.avatar {
  width: 112rpx; height: 112rpx; border-radius: 50%;
  background: rgba(255, 255, 255, 0.22);
  border: 2rpx solid rgba(255, 255, 255, 0.5);
  display: flex; align-items: center; justify-content: center;
  font-size: 52rpx; font-weight: 700;
}
.who .name { display: block; font-size: 38rpx; font-weight: 700; }
.who .role {
  display: inline-block; margin-top: 10rpx; font-size: 22rpx;
  background: rgba(255, 255, 255, 0.2); border-radius: 999rpx; padding: 4rpx 20rpx;
}
.cells {
  background: #fff; border-radius: 24rpx; margin: -48rpx 28rpx 24rpx;
  box-shadow: 0 8rpx 28rpx rgba(31, 35, 41, 0.07); overflow: hidden;
}
.cells + .cells { margin-top: 24rpx; }
.cell {
  display: flex; align-items: center; padding: 32rpx 32rpx;
  border-bottom: 2rpx solid #f0f2f7;
}
.cell:last-child { border-bottom: none; }
.cell:active { background: #f7f9ff; }
.cell .t { flex: 1; font-size: 30rpx; color: #1f2329; }
.cell .v { font-size: 26rpx; color: #8a919f; margin-right: 12rpx; }
.cell .arrow { font-size: 36rpx; color: #c2c8d2; }
.cell.danger .t { color: #e64545; }
.foot { text-align: center; color: #a2a9b5; font-size: 24rpx; margin-top: 48rpx; }
</style>
