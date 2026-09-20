import { defineConfig, loadEnv } from "vite";
import uni from "@dcloudio/vite-plugin-uni";

const uniPlugin = uni.default || uni;

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  return {
    plugins: [uniPlugin()],
    define: {
      __VITE_API_BASE__: JSON.stringify(env.VITE_API_BASE || ""),
      __VITE_WS_URL__: JSON.stringify(env.VITE_WS_URL || "ws://127.0.0.1:8000/ws/notify"),
      __H5_VER_CODE__: JSON.stringify(env.H5_VER_CODE || "100"),   // 与 manifest versionCode 同步
      __APP_VER_CODE__: JSON.stringify(env.APP_VER_CODE || "100"), // 同上
    },
    server: {
      port: 5173,
      proxy: {
        "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
        "/ws": { target: "ws://127.0.0.1:8000", ws: true },
      },
    },
  };
});