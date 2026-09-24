import { defineConfig, loadEnv } from "vite";
import uni from "@dcloudio/vite-plugin-uni";

const uniPlugin = uni.default || uni;

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  // 默认打本机后端；并行验证改动时可指向另一端口（VITE_PROXY_TARGET=http://127.0.0.1:18081）
  const target = env.VITE_PROXY_TARGET || "http://127.0.0.1:18080";
  return {
    plugins: [uniPlugin()],
    define: {
      __VITE_API_BASE__: JSON.stringify(env.VITE_API_BASE || ""),
      __VITE_WS_URL__: JSON.stringify(env.VITE_WS_URL || `${target.replace(/^http/, "ws")}/ws/notify`),
      __H5_VER_CODE__: JSON.stringify(env.H5_VER_CODE || "100"),   // 与 manifest versionCode 同步
      __APP_VER_CODE__: JSON.stringify(env.APP_VER_CODE || "100"), // 同上
    },
    server: {
      port: 5173,
      proxy: {
        "/api": { target, changeOrigin: true },
        "/ws": { target: target.replace(/^http/, "ws"), ws: true },
      },
    },
  };
});